"""動画生成プロバイダーの非同期アダプター。

このモジュールは、課金を伴う ``start`` を一度だけ送る小さな境界です。API
レスポンス本文、トークン、署名付き出力 URL は例外・ログ向けの文字列に含めません。
プロジェクト DB に URL を保存するかどうかは呼出側の責務であり、このモジュールは
``ProviderState`` を返すだけです。
"""
from __future__ import annotations

import base64
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .errors import SubmissionUnknown, VideoError
from .logging_safety import install_httpx_filter

install_httpx_filter()

_REPLICATE_API = "https://api.replicate.com/v1"
_RUNWAY_API = "https://api.dev.runwayml.com/v1"
_RUNWAY_VERSION = "2024-11-06"
_REPLICATE_MODEL = "kwaivgi/kling-v2.1"
_RUNWAY_MODEL = "gen4_turbo"

# 入力画像は親サービスが準備した PNG のみを使う。アダプターが勝手に JPEG 化又は
# リサイズすると、呼出側が記録する構図・向きと実際の入力が食い違うためである。
_MAX_START_IMAGE_BYTES = 1 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_PROVIDER_ID_LENGTH = 128
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_REPLICATE_CDN_SUFFIX = "replicate.delivery"
_RUNWAY_CDN_HOST = "dnznrvs05pmza.cloudfront.net"

ProviderStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]


@dataclass(frozen=True, slots=True)
class ProviderState:
    """ポーリング結果。``output_url`` は成功時だけの短命なメモリ上の値である。"""

    status: ProviderStatus
    output_url: str | None = None


def _error_not_configured() -> VideoError:
    return VideoError(
        "provider_not_configured",
        "動画プロバイダーの認証情報が設定されていないか、この認証方式には対応していません。",
        503,
    )


def _error_rejected() -> VideoError:
    return VideoError(
        "provider_rejected",
        "動画プロバイダーが生成リクエストを受け付けませんでした。入力を確認して再準備してください。",
        422,
    )


def _error_unavailable() -> VideoError:
    return VideoError(
        "provider_unavailable",
        "動画プロバイダーを現在利用できません。認証設定またはサービス状態を確認してください。",
        503,
    )


def _error_protocol() -> VideoError:
    return VideoError(
        "provider_protocol_error",
        "動画プロバイダーから安全に処理できない応答を受け取りました。",
        502,
    )


def _error_invalid_output() -> VideoError:
    return VideoError(
        "invalid_provider_output",
        "動画プロバイダー出力を安全な MP4 として確認できませんでした。",
        502,
    )


def _has_secret(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_provider_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > _MAX_PROVIDER_ID_LENGTH:
        return None
    return value if _PROVIDER_ID_RE.fullmatch(value) else None


def _native_replicate_duration(seconds: float) -> int:
    # Kling v2.1 on Replicate exposes only 5 and 10 second native durations.
    return 5 if math.ceil(seconds) <= 5 else 10


def _native_runway_duration(seconds: float) -> int:
    # The native API accepts integral 2--10 seconds.
    return max(2, math.ceil(seconds))


def _project_fields(project: dict[str, Any], *, require_ratio: bool) -> tuple[str, float, str | None]:
    """副作用なしで、共通 project 辞書からこのアダプターの入力を読む。"""

    if not isinstance(project, dict):
        raise VideoError("invalid_project", "プロジェクト情報が不正です。", 422)

    prompt = project.get("render_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise VideoError("invalid_project", "動画生成用のプロンプトが不正です。", 422)

    duration = project.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise VideoError("invalid_project", "動画の長さが不正です。", 422)
    duration_value = float(duration)
    if not math.isfinite(duration_value) or duration_value <= 0 or duration_value > 10:
        raise VideoError(
            "invalid_duration",
            "AI 動画の 1 回の生成は 10 秒以下にしてください。長い動画はシーンに分割してください。",
            422,
        )

    ratio = project.get("aspect_ratio")
    if require_ratio and ratio not in {"16:9", "9:16"}:
        raise VideoError("invalid_project", "動画のアスペクト比が不正です。", 422)
    if ratio is not None and ratio not in {"16:9", "9:16"}:
        raise VideoError("invalid_project", "動画のアスペクト比が不正です。", 422)
    return prompt.strip(), duration_value, ratio if isinstance(ratio, str) else None


def _png_data_uri(image_path: Path) -> str:
    """親で準備した小さな PNG だけを data URI にする。"""

    path = Path(image_path)
    try:
        size = path.stat().st_size
        if not path.is_file() or size <= 0:
            raise OSError
        if size > _MAX_START_IMAGE_BYTES:
            raise VideoError(
                "image_too_large",
                "開始画像が 1 MiB を超えています。640x360 程度の PNG として再準備してください。",
                422,
            )
        data = path.read_bytes()
    except VideoError:
        raise
    except OSError:
        raise VideoError("invalid_image", "開始画像ファイルを読み取れません。", 422) from None

    # 拡張子だけを信頼せず、PNG signature も確認する。入力は raw PNG で最大 1 MiB。
    if path.suffix.lower() != ".png" or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VideoError("invalid_image", "開始画像は 1 MiB 以下の PNG で再準備してください。", 422)
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _allowed_asset_url(url: object, *, provider: str) -> bool:
    """出力 URL を固定 CDN に限定して SSRF と認証転送を防ぐ。"""

    if not isinstance(url, str) or not url or len(url) > 8192:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None or port not in (None, 443):
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if provider == "replicate":
        return host == _REPLICATE_CDN_SUFFIX or host.endswith("." + _REPLICATE_CDN_SUFFIX)
    return host == _RUNWAY_CDN_HOST


def _output_url(payload: Mapping[str, Any], *, provider: str) -> str:
    """成功結果の URL を読むが、不明な URL は DB に渡す前に拒否する。"""

    output: object = payload.get("output")
    if isinstance(output, (list, tuple)):
        output = output[0] if output else None
    if not _allowed_asset_url(output, provider=provider):
        raise _error_invalid_output()
    return output  # type: ignore[return-value]


class _AsyncProvider:
    """HTTP client を注入可能にする、ネットワークなしの基底クラス。"""

    name: Literal["replicate", "runway"]

    def __init__(
        self,
        settings: Settings,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._token = token
        self._transport = transport
        configured_cap = getattr(settings, "max_video_bytes", _MAX_DOWNLOAD_BYTES)
        # Settings の値が大きくても 256 MiB を超えない。小さい値はさらに厳しく尊重する。
        self._download_cap = min(_MAX_DOWNLOAD_BYTES, max(1, int(configured_cap)))

    def __repr__(self) -> str:
        # token、Settings 内の api_token、transport を repr に混ぜない。
        return f"{self.__class__.__name__}(name={self.name!r})"

    def _new_client(self) -> httpx.AsyncClient:
        # Cookie jar は新規かつ空。follow_redirects=False は API・CDN の双方で必須。
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
        )

    def _require_token(self) -> str:
        if not _has_secret(self._token):
            raise _error_not_configured()
        return self._token.strip()  # type: ignore[union-attr]

    @staticmethod
    def _start_status_error(status_code: int) -> VideoError | SubmissionUnknown:
        # POST がサーバー又はネットワークに到達した後の 5xx は、受付済みか判別できない。
        if status_code >= 500 or status_code < 100:
            return SubmissionUnknown()
        if status_code in {401, 403, 429}:
            return _error_unavailable()
        if 400 <= status_code < 500:
            return _error_rejected()
        return SubmissionUnknown()

    @staticmethod
    def _poll_status_error(status_code: int) -> VideoError:
        if status_code in {401, 403, 429} or status_code >= 500:
            return _error_unavailable()
        return _error_protocol()

    async def _submit_json(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        """一回だけの start POST。曖昧な結果では絶対に再送しない。"""

        try:
            async with self._new_client() as client:
                response = await client.post(url, headers=headers, json=payload)
        except Exception:
            raise SubmissionUnknown() from None

        if not 200 <= response.status_code < 300:
            raise self._start_status_error(response.status_code)
        try:
            decoded = response.json()
        except Exception:
            raise SubmissionUnknown() from None
        if not isinstance(decoded, dict):
            raise SubmissionUnknown()
        return decoded

    async def _poll_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        try:
            async with self._new_client() as client:
                response = await client.get(url, headers=headers)
        except Exception:
            raise _error_unavailable() from None
        if not 200 <= response.status_code < 300:
            raise self._poll_status_error(response.status_code)
        try:
            decoded = response.json()
        except Exception:
            raise _error_protocol() from None
        if not isinstance(decoded, dict):
            raise _error_protocol()
        return decoded

    async def download(self, url: str, destination: Path) -> None:
        """短命な出力 URL から、無認証ストリームを原子的に MP4 保存する。"""

        if not _allowed_asset_url(url, provider=self.name):
            raise _error_invalid_output()
        destination = Path(destination)
        if destination.suffix.lower() != ".mp4":
            raise VideoError("invalid_destination", "出力先は .mp4 ファイルにしてください。", 422)

        temporary_name: str | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # ここでは API client を共有しない。Authorization と cookies を一切渡さない。
            async with self._new_client() as client:
                async with client.stream("GET", url, headers={"Accept": "video/mp4"}) as response:
                    if response.status_code != 200:
                        raise _error_invalid_output()
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            declared_size = int(content_length)
                        except (TypeError, ValueError):
                            raise _error_invalid_output() from None
                        if declared_size < 0 or declared_size > self._download_cap:
                            raise _error_invalid_output()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type and content_type != "video/mp4":
                        raise _error_invalid_output()

                    total = 0
                    header = bytearray()
                    with tempfile.NamedTemporaryFile(
                        mode="wb", suffix=".mp4", dir=destination.parent, delete=False
                    ) as temporary:
                        temporary_name = temporary.name
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            if not isinstance(chunk, bytes):
                                raise _error_invalid_output()
                            total += len(chunk)
                            if total > self._download_cap:
                                raise _error_invalid_output()
                            if len(header) < 16:
                                header.extend(chunk[: 16 - len(header)])
                            temporary.write(chunk)

            # ストリーム先頭を検査するため、HTML/JSON を video として公開しない。
            if total < 8 or header[4:8] != b"ftyp":
                raise _error_invalid_output()
            os.replace(temporary_name, destination)
            temporary_name = None
        except VideoError:
            raise
        except Exception:
            # CDN の署名 URL、レスポンス本文、例外内の URL を外へ出さない。
            raise _error_invalid_output() from None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass


class ReplicateKlingProvider(_AsyncProvider):
    """Replicate 経由の ``kwaivgi/kling-v2.1`` image-to-video アダプター。"""

    name: Literal["replicate"] = "replicate"

    def __init__(
        self,
        settings: Settings,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(settings, token if token is not None else os.getenv("REPLICATE_API_TOKEN"), transport=transport)

    def validate(self, project: dict[str, Any]) -> None:
        """課金も I/O も行わず、Kling の 5/10 秒に丸め可能かだけ確認する。"""

        _prompt, duration, _ratio = _project_fields(project, require_ratio=False)
        _native_replicate_duration(duration)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def start(self, project: dict[str, Any], image_path: Path) -> str:
        self.validate(project)
        prompt, duration, _ratio = _project_fields(project, require_ratio=False)
        image_uri = _png_data_uri(Path(image_path))
        payload = {
            "input": {
                "prompt": prompt,
                "start_image": image_uri,
                "mode": "standard",
                "duration": _native_replicate_duration(duration),
            }
        }
        result = await self._submit_json(
            f"{_REPLICATE_API}/models/{_REPLICATE_MODEL}/predictions",
            headers=self._headers(),
            payload=payload,
        )
        provider_id = _safe_provider_id(result.get("id"))
        if provider_id is None:
            # provider が開始した可能性を否定できないが、安全に追跡する ID がない。
            raise SubmissionUnknown()
        return provider_id

    async def poll(self, provider_job_id: str) -> ProviderState:
        provider_id = _safe_provider_id(provider_job_id)
        if provider_id is None:
            raise VideoError("invalid_provider_job", "動画プロバイダーのジョブ ID が不正です。", 422)
        result = await self._poll_json(
            f"{_REPLICATE_API}/predictions/{quote(provider_id, safe='')}",
            headers={"Authorization": f"Bearer {self._require_token()}", "Accept": "application/json"},
        )
        if result.get("id") != provider_id:
            raise _error_protocol()
        status = result.get("status")
        mapped = {
            "starting": "queued",
            "processing": "running",
            "succeeded": "succeeded",
            "failed": "failed",
            "canceled": "canceled",
            "cancelled": "canceled",
            "aborted": "canceled",
        }.get(status)
        if mapped is None:
            raise _error_protocol()
        if mapped == "succeeded":
            return ProviderState("succeeded", _output_url(result, provider=self.name))
        return ProviderState(mapped)  # type: ignore[arg-type]


class RunwayProvider(_AsyncProvider):
    """Runway の固定 ``gen4_turbo`` image-to-video アダプター。"""

    name: Literal["runway"] = "runway"

    def __init__(
        self,
        settings: Settings,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(settings, token if token is not None else os.getenv("RUNWAY_API_KEY"), transport=transport)

    def validate(self, project: dict[str, Any]) -> None:
        """課金も I/O も行わず、Runway の整数 2--10 秒への丸めを確認する。"""

        _prompt, duration, _ratio = _project_fields(project, require_ratio=True)
        _native_runway_duration(duration)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._require_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Runway-Version": _RUNWAY_VERSION,
        }

    async def start(self, project: dict[str, Any], image_path: Path) -> str:
        self.validate(project)
        prompt, duration, ratio = _project_fields(project, require_ratio=True)
        image_uri = _png_data_uri(Path(image_path))
        payload = {
            "model": _RUNWAY_MODEL,
            "promptImage": image_uri,
            "promptText": prompt,
            "ratio": "1280:720" if ratio == "16:9" else "720:1280",
            "duration": _native_runway_duration(duration),
        }
        result = await self._submit_json(
            f"{_RUNWAY_API}/image_to_video",
            headers=self._headers(),
            payload=payload,
        )
        provider_id = _safe_provider_id(result.get("id"))
        if provider_id is None:
            raise SubmissionUnknown()
        return provider_id

    async def poll(self, provider_job_id: str) -> ProviderState:
        provider_id = _safe_provider_id(provider_job_id)
        if provider_id is None:
            raise VideoError("invalid_provider_job", "動画プロバイダーのジョブ ID が不正です。", 422)
        result = await self._poll_json(
            f"{_RUNWAY_API}/tasks/{quote(provider_id, safe='')}",
            headers={
                "Authorization": f"Bearer {self._require_token()}",
                "Accept": "application/json",
                "X-Runway-Version": _RUNWAY_VERSION,
            },
        )
        # Runway task detail documents this id; mismatches must not alter another job.
        if result.get("id") != provider_id:
            raise _error_protocol()
        status = result.get("status")
        mapped = {
            "PENDING": "queued",
            "THROTTLED": "queued",
            "RUNNING": "running",
            "SUCCEEDED": "succeeded",
            "FAILED": "failed",
            "CANCELED": "canceled",
            "CANCELLED": "canceled",
            "ABORTED": "canceled",
        }.get(status)
        if mapped is None:
            raise _error_protocol()
        if mapped == "succeeded":
            return ProviderState("succeeded", _output_url(result, provider=self.name))
        return ProviderState(mapped)  # type: ignore[arg-type]


def select_provider(name: str, settings: Settings) -> ReplicateKlingProvider | RunwayProvider:
    """設定済みのアダプターだけを明示的に選択する。

    ``auto`` は Replicate (Kling) を優先し、次に Runway を選ぶ。直接 Kling
    は endpoint・出力 host 等を未検証のため実装しておらず、``KLING_API_KEY``
    単独では安全に 503 を返す。
    """

    replicate_token = os.getenv("REPLICATE_API_TOKEN")
    runway_token = os.getenv("RUNWAY_API_KEY")

    if name == "auto":
        if _has_secret(replicate_token):
            return ReplicateKlingProvider(settings, replicate_token)
        if _has_secret(runway_token):
            return RunwayProvider(settings, runway_token)
        raise _error_not_configured()
    if name == "replicate":
        if _has_secret(replicate_token):
            return ReplicateKlingProvider(settings, replicate_token)
        raise _error_not_configured()
    if name == "runway":
        if _has_secret(runway_token):
            return RunwayProvider(settings, runway_token)
        raise _error_not_configured()
    if name == "kling":
        # スキーマ上の kling 指定は、設定済み Replicate Kling に安全に解決できる。
        # KLING_API_KEY に対して推測した native endpoint を呼ぶことはしない。
        if _has_secret(replicate_token):
            return ReplicateKlingProvider(settings, replicate_token)
        raise _error_not_configured()
    raise _error_not_configured()


# 呼出側の命名差を吸収する安全な公開別名。どちらも同じ固定実装を指す。
ReplicateKlingAdapter = ReplicateKlingProvider
RunwayAdapter = RunwayProvider

__all__ = [
    "ProviderState",
    "ReplicateKlingAdapter",
    "ReplicateKlingProvider",
    "RunwayAdapter",
    "RunwayProvider",
    "select_provider",
]
