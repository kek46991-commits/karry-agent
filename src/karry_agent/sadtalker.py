"""Hugging Face Space 上の SadTalker を安全に呼ぶ非同期アダプタ。

このモジュールはローカル推論を行わない。同期的な Gradio の待機・キュー処理は、
短命な :mod:`karry_agent.sadtalker_worker` プロセスに閉じ込める。
"""

from __future__ import annotations

import asyncio
from enum import Enum
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
from typing import Final


DEFAULT_API_NAME: Final = "/test"
DEFAULT_TIMEOUT_SECONDS: Final = 90
MIN_TIMEOUT_SECONDS: Final = 5
MAX_TIMEOUT_SECONDS: Final = 180
MAX_OUTPUT_BYTES: Final = 60 * 1024 * 1024
MAX_INPUT_BYTES: Final = 32 * 1024 * 1024
_PROCESS_STOP_SECONDS: Final = 3.0
_SAFE_JSON_LIMIT: Final = 4096

# HF の Space ID は URL ではなく ``owner/name`` だけを受け入れる。
_SPACE_ID_RE: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$"
)
_API_NAME_RE: Final = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")


class SadTalkerErrorCode(str, Enum):
    """呼出し元へ出してよい、固定された失敗分類。"""

    CONFIGURATION = "sadtalker_configuration"
    BUSY = "sadtalker_busy"
    TIMEOUT = "sadtalker_timeout"
    UNAVAILABLE = "sadtalker_unavailable"


_ERROR_MESSAGES: Final = {
    SadTalkerErrorCode.CONFIGURATION: "SadTalker の設定が無効です。",
    SadTalkerErrorCode.BUSY: "SadTalker は現在利用中です。",
    SadTalkerErrorCode.TIMEOUT: "SadTalker の応答が時間切れになりました。",
    SadTalkerErrorCode.UNAVAILABLE: "SadTalker を現在利用できません。",
}


class SadTalkerError(RuntimeError):
    """秘密情報やプロバイダ詳細を含まない SadTalker 例外。"""

    def __init__(self, code: SadTalkerErrorCode | str) -> None:
        self.code = SadTalkerErrorCode(code)
        super().__init__(_ERROR_MESSAGES[self.code])


# Render のワーカープロセスを一つだけにする。acquire(False) により待ち行列を作らない。
_EXTERNAL_JOB_GUARD = threading.BoundedSemaphore(1)


def _clean_space_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _SPACE_ID_RE.fullmatch(candidate):
        return None
    return candidate


def _clean_api_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _API_NAME_RE.fullmatch(candidate) or "//" in candidate or ".." in candidate:
        return None
    return candidate


def _timeout_from(value: object) -> int:
    """環境変数の値を安全な有限範囲へ丸める。"""

    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = DEFAULT_TIMEOUT_SECONDS
    return min(MAX_TIMEOUT_SECONDS, max(MIN_TIMEOUT_SECONDS, parsed))


def configuration_from_env() -> dict[str, bool | int | str]:
    """公開してよい最小限の設定状態を返す。秘密値は決して返さない。"""

    space_id = _clean_space_id(os.environ.get("KARRY_SADTALKER_SPACE", ""))
    api_name = _clean_api_name(os.environ.get("KARRY_SADTALKER_API_NAME", DEFAULT_API_NAME))
    return {
        "configured": bool(space_id and api_name),
        "timeout_seconds": _timeout_from(os.environ.get("KARRY_SADTALKER_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
        "provider": "huggingface",
    }


class SadTalkerAdapter:
    """画像と WAV を HF Gradio SadTalker Space へ送る非同期アダプタ。"""

    def __init__(
        self,
        space_id: str | None = None,
        *,
        api_name: str = DEFAULT_API_NAME,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.space_id = _clean_space_id(space_id) if space_id is not None else None
        self.api_name = _clean_api_name(api_name)
        self.timeout_seconds = _timeout_from(timeout_seconds)
        # 明示指定で URL 等を渡した場合も、ネットワークへ出ず generate 時に安全に失敗する。
        self._configuration_valid = (space_id is None or self.space_id is not None) and self.api_name is not None

    @property
    def configured(self) -> bool:
        return bool(self._configuration_valid and self.space_id and self.api_name)

    async def generate(self, image: bytes, audio: bytes) -> bytes:
        """SadTalker の MP4 を返す。待機中でも event loop はブロックしない。"""

        if not self._configuration_valid:
            raise SadTalkerError(SadTalkerErrorCode.CONFIGURATION)
        if not self.configured:
            raise SadTalkerError(SadTalkerErrorCode.CONFIGURATION)
        if not isinstance(image, bytes) or not isinstance(audio, bytes):
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
        if not image or not audio or len(image) > MAX_INPUT_BYTES or len(audio) > MAX_INPUT_BYTES:
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
        if not _EXTERNAL_JOB_GUARD.acquire(blocking=False):
            raise SadTalkerError(SadTalkerErrorCode.BUSY)

        try:
            with tempfile.TemporaryDirectory(prefix="karry-sadtalker-") as temporary:
                root = Path(temporary)
                input_dir = root / "input"
                output_dir = root / "output"
                input_dir.mkdir(mode=0o700)
                output_dir.mkdir(mode=0o700)
                image_path = input_dir / "source-image"
                audio_path = input_dir / "driven-audio.wav"
                _write_private_file(image_path, image)
                _write_private_file(audio_path, audio)

                request = {
                    "image_path": str(image_path),
                    "audio_path": str(audio_path),
                    "output_dir": str(output_dir),
                    "space_id": self.space_id,
                    "api_name": self.api_name,
                    "timeout_seconds": self.timeout_seconds,
                }
                response = await self._run_worker(request)
                if response.get("ok") is not True:
                    raise SadTalkerError(_response_code(response))
                # 子プロセスの出力パスを採用しない。親が決めた名前だけを読める。
                if response != {"ok": True, "output_name": "result.mp4"}:
                    raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
                return _read_verified_mp4(output_dir / "result.mp4")
        except SadTalkerError:
            raise
        except asyncio.CancelledError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE) from None
        finally:
            _EXTERNAL_JOB_GUARD.release()

    async def _run_worker(self, request: dict[str, object]) -> dict[str, object]:
        """限定環境の短命な子プロセスを実行し、固定サイズの JSON だけを受け取る。"""

        payload = json.dumps(request, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                # ``-m karry_agent...`` は package __init__ の無関係なSDKまで import
                # し得るため、短命worker自身だけを直接起動する。
                str(Path(__file__).with_name("sadtalker_worker.py")),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=_worker_environment(),
                # 依存モジュールを見付けるための package root 以外の cwd は不要。
                cwd=str(Path(__file__).resolve().parents[2]),
                limit=_SAFE_JSON_LIMIT,
            )
        except (OSError, ValueError):
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE) from None

        communicate = asyncio.create_task(process.communicate(payload))
        try:
            stdout, _ = await asyncio.wait_for(
                asyncio.shield(communicate), timeout=float(self.timeout_seconds)
            )
        except TimeoutError:
            await _stop_worker(process, communicate)
            raise SadTalkerError(SadTalkerErrorCode.TIMEOUT) from None
        except asyncio.CancelledError:
            await _stop_worker(process, communicate)
            raise
        except (OSError, ValueError):
            await _stop_worker(process, communicate)
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE) from None

        if process.returncode != 0 or len(stdout) > _SAFE_JSON_LIMIT:
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
        return _parse_safe_response(stdout)


def adapter_from_env() -> SadTalkerAdapter:
    """環境変数からアダプタを作る。未設定でもローカル fallback 用に安全な状態で返す。"""

    return SadTalkerAdapter(
        os.environ.get("KARRY_SADTALKER_SPACE"),
        api_name=os.environ.get("KARRY_SADTALKER_API_NAME", DEFAULT_API_NAME),
        timeout_seconds=_timeout_from(os.environ.get("KARRY_SADTALKER_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
    )


def _worker_environment() -> dict[str, str]:
    """子プロセスに渡す環境を allow-list する。KARRY_* や認証情報はコピーしない。"""

    package_root = str(Path(__file__).resolve().parents[1])
    inherited_path = os.environ.get("PYTHONPATH", "")
    pythonpath = package_root if not inherited_path else package_root + os.pathsep + inherited_path
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": pythonpath,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    token = os.environ.get("HF_TOKEN")
    if token:
        environment["HF_TOKEN"] = token
    return environment


def _write_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # fdopen が失敗した場合を含め、TemporaryDirectory の外へ何も残さない。
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


async def _stop_worker(
    process: asyncio.subprocess.Process,
    communicate: "asyncio.Task[tuple[bytes, bytes | None]]",
) -> None:
    """terminate → bounded wait → kill → bounded wait を cancellation/timeout 時にも行う。"""

    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_STOP_SECONDS)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=_PROCESS_STOP_SECONDS)
            except TimeoutError:
                # communicate を止めて親の task を永久に残さない。
                pass

    if not communicate.done():
        try:
            await asyncio.wait_for(asyncio.shield(communicate), timeout=_PROCESS_STOP_SECONDS)
        except (TimeoutError, OSError, ValueError):
            communicate.cancel()
            try:
                await communicate
            except (asyncio.CancelledError, OSError, ValueError):
                pass


def _parse_safe_response(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > _SAFE_JSON_LIMIT:
        raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE) from None
    if not isinstance(value, dict) or set(value) - {"ok", "output_name", "code"}:
        raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
    return value


def _response_code(response: dict[str, object]) -> SadTalkerErrorCode:
    # 子から来る文字列は strict allow-list に戻す。任意の provider message は扱わない。
    if set(response) != {"ok", "code"} or response.get("ok") is not False:
        return SadTalkerErrorCode.UNAVAILABLE
    return SadTalkerErrorCode.CONFIGURATION if response.get("code") == "configuration" else SadTalkerErrorCode.UNAVAILABLE


def _read_verified_mp4(path: Path) -> bytes:
    """親の private output dir にある通常ファイルだけを bounded read する。"""

    try:
        listed = os.lstat(path)
        if not stat.S_ISREG(listed.st_mode) or stat.S_ISLNK(listed.st_mode):
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
        if listed.st_size < 12 or listed.st_size > MAX_OUTPUT_BYTES or listed.st_nlink != 1:
            raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_ino != listed.st_ino
                or opened.st_dev != listed.st_dev
                or opened.st_size != listed.st_size
                or opened.st_nlink != 1
            ):
                raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                data = stream.read(MAX_OUTPUT_BYTES + 1)
            descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except SadTalkerError:
        raise
    except OSError:
        raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE) from None

    if len(data) != listed.st_size or len(data) > MAX_OUTPUT_BYTES or data[4:8] != b"ftyp":
        raise SadTalkerError(SadTalkerErrorCode.UNAVAILABLE)
    return data


__all__ = [
    "SadTalkerAdapter",
    "SadTalkerError",
    "SadTalkerErrorCode",
    "adapter_from_env",
    "configuration_from_env",
]
