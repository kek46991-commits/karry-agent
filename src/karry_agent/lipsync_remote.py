"""Talking-head (lip-sync) adapters: one photo + narration WAV → MP4 of that person speaking.

SadTalker / LivePortrait-class models need a GPU, so they never run inside the CPU-only
Render instance. The owner points karry-agent at an endpoint they control:

* ``http``   – any service implementing the tiny contract in ``tools/lipsync_server``
               (multipart ``image`` + ``audio`` → ``video/mp4``), e.g. SadTalker on a free Colab GPU.
* ``gradio`` – a SadTalker Gradio app (e.g. a Hugging Face Space duplicated to the owner's
               account) driven through Gradio's documented HTTP API (upload → call → SSE result).

Disabled unless ``KARRY_LIPSYNC_PROVIDER`` is set; there is no paid-API fallback. The endpoint
URL and token are never returned to the UI or put into error messages.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from .video_suite import MAX_CLIP_BYTES, VideoError

MAX_FACE_SIDE = 1024
DEFAULT_TIMEOUT = 900
MAX_TIMEOUT = 3600
DEFAULT_GRADIO_API = "/test"
# Canonical SadTalker ``/test`` inputs after (image, audio): preprocess, still_mode, use_enhancer, batch_size, size, pose_style.
DEFAULT_GRADIO_ARGS: list[Any] = ["crop", False, False, 2, 256, 0]
_SLOT = threading.BoundedSemaphore(1)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class LipSyncAdapter(Protocol):
    name: str

    def animate(self, face_png: bytes, narration_wav: bytes) -> bytes: ...


def _config_error(message: str) -> VideoError:
    return VideoError(message, 503, "lipsync_configuration")


def _origin(url: str) -> tuple[str, str]:
    parsed = urlsplit((url or "").strip())
    host = (parsed.hostname or "").lower()
    secure = parsed.scheme == "https" or (parsed.scheme == "http" and host in _LOCAL_HOSTS)
    if not host or not secure or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise _config_error("人物アニメーションの実行先URL（KARRY_LIPSYNC_URL）は https:// で指定してください。")
    return f"{parsed.scheme}://{parsed.netloc}", parsed.path.rstrip("/")


def _timeout() -> int:
    try:
        return max(60, min(MAX_TIMEOUT, int(os.getenv("KARRY_LIPSYNC_TIMEOUT", DEFAULT_TIMEOUT))))
    except ValueError:
        return DEFAULT_TIMEOUT


def prepare_face(body: bytes) -> bytes:
    """Validate the uploaded photo and re-encode it as an upright PNG of at most 1024 px."""
    try:
        with Image.open(io.BytesIO(body)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        raise VideoError("写真を読み込めませんでした。JPG / PNG / WEBP を選んでください。", 422, "invalid_image") from None
    image.thumbnail((MAX_FACE_SIDE, MAX_FACE_SIDE))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _is_mp4(body: bytes) -> bool:
    return len(body) > 12 and body[4:8] == b"ftyp"


class _Endpoint:
    name = ""

    def __init__(self, url: str, token: str = "", session: Any | None = None, timeout: int | None = None):
        self.origin, self.path = _origin(url)
        if any(ch in token for ch in "\r\n"):
            raise _config_error("KARRY_LIPSYNC_TOKEN に改行が含まれています。")
        self.headers = {"Authorization": "Bearer " + token} if token else {}
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.timeout = timeout or _timeout()

    def _request(self, method: str, url: str, **kwargs: Any):
        try:
            response = self.session.request(method, url, headers={**self.headers, **kwargs.pop("headers", {})},
                                            timeout=kwargs.pop("timeout", (10, 60)), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise VideoError("人物アニメーションの実行先に接続できません。", 502, "lipsync_unavailable") from None
        if response.status_code in {401, 403}:
            raise VideoError("人物アニメーションの実行先に拒否されました（トークンを確認してください）。", 502, "lipsync_rejected")
        if response.status_code == 429:
            raise VideoError("人物アニメーションの実行先が混み合っています。", 502, "lipsync_busy")
        if response.status_code >= 400:
            raise VideoError("人物アニメーションの実行先でエラーになりました。", 502, "lipsync_failed")
        return response

    def _read_video(self, response: Any, deadline: float) -> bytes:
        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_CLIP_BYTES:
                raise VideoError("アニメーション動画の容量上限を超えました。", 413, "output_capacity")
            if time.monotonic() > deadline:
                raise VideoError("人物アニメーションが時間内に終わりませんでした。", 504, "lipsync_timeout")
        if not _is_mp4(bytes(body)):
            raise VideoError("人物アニメーションの結果が MP4 ではありませんでした。", 502, "lipsync_response")
        return bytes(body)

    def animate(self, face_png: bytes, narration_wav: bytes) -> bytes:
        if not _SLOT.acquire(timeout=self.timeout):
            raise VideoError("ほかの人物アニメーションが実行中です。", 503, "lipsync_busy")
        try:
            return self._animate(face_png, narration_wav, time.monotonic() + self.timeout)
        finally:
            _SLOT.release()

    def _animate(self, face_png: bytes, narration_wav: bytes, deadline: float) -> bytes:
        raise NotImplementedError


class HttpLipSyncAdapter(_Endpoint):
    """POST multipart ``image`` (PNG) + ``audio`` (WAV) to the owner's endpoint; the response body is the MP4."""

    name = "http"

    def _animate(self, face_png: bytes, narration_wav: bytes, deadline: float) -> bytes:
        files = {"image": ("face.png", face_png, "image/png"), "audio": ("narration.wav", narration_wav, "audio/wav")}
        with self._request("POST", self.origin + self.path, files=files, stream=True,
                           headers={"Accept": "video/mp4"}, timeout=(10, self.timeout)) as response:
            if response.status_code != 200:
                raise VideoError("人物アニメーションの実行先の応答を確認できませんでした。", 502, "lipsync_response")
            return self._read_video(response, deadline)


class GradioLipSyncAdapter(_Endpoint):
    """SadTalker-style Gradio app (Gradio 4/5 HTTP API: ``/upload`` → ``/call/<api>`` → SSE ``complete``)."""

    name = "gradio"

    def __init__(self, url: str, token: str = "", api_name: str = DEFAULT_GRADIO_API, extra_args: list[Any] | None = None, **kwargs: Any):
        super().__init__(url, token, **kwargs)
        if not api_name.startswith("/") or not api_name[1:].replace("_", "").isalnum():
            raise _config_error("KARRY_LIPSYNC_GRADIO_API は /test のように指定してください。")
        self.api_name = api_name
        self.extra_args = list(DEFAULT_GRADIO_ARGS if extra_args is None else extra_args)

    def _prefix(self) -> str:
        try:
            config = self._request("GET", f"{self.origin}{self.path}/config").json()
        except ValueError:
            raise VideoError("人物アニメーションの実行先（Gradio）の設定を読めませんでした。", 502, "lipsync_response") from None
        prefix = config.get("api_prefix") or "" if isinstance(config, dict) else ""
        return self.path + (prefix if isinstance(prefix, str) and prefix.startswith("/") else "")

    def _upload(self, base: str, name: str, body: bytes, mime: str) -> dict[str, Any]:
        try:
            paths = self._request("POST", base + "/upload", files={"files": (name, body, mime)}).json()
        except ValueError:
            paths = None
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], str):
            raise VideoError("人物アニメーションの実行先に写真・音声を送れませんでした。", 502, "lipsync_response")
        return {"path": paths[0], "orig_name": name, "meta": {"_type": "gradio.FileData"}}

    def _result(self, base: str, event_id: str, deadline: float) -> Any:
        url = f"{self.origin}{base}/call{self.api_name}/{event_id}"
        with self._request("GET", url, stream=True, timeout=(10, self.timeout)) as response:
            event = None
            for raw in response.iter_lines(decode_unicode=True):
                if time.monotonic() > deadline:
                    raise VideoError("人物アニメーションが時間内に終わりませんでした。", 504, "lipsync_timeout")
                line = (raw or "").strip()
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event in {"complete", "error"}:
                    if event == "error":
                        raise VideoError("人物アニメーションの実行先で処理に失敗しました（顔が写った写真か確認してください）。", 502, "lipsync_failed")
                    try:
                        return json.loads(line[5:])
                    except ValueError:
                        break
        raise VideoError("人物アニメーションの実行先の応答を確認できませんでした。", 502, "lipsync_response")

    def _output_url(self, base: str, data: Any) -> str:
        item = data[0] if isinstance(data, list) and data else None
        if isinstance(item, dict) and isinstance(item.get("video"), dict):
            item = item["video"]
        if not isinstance(item, dict):
            raise VideoError("人物アニメーションの結果に動画がありません。", 502, "output_missing")
        url = item.get("url") or (f"{self.origin}{base}/file={item['path']}" if isinstance(item.get("path"), str) else "")
        if not isinstance(url, str) or urlsplit(url).scheme + "://" + urlsplit(url).netloc != self.origin:
            # The token is only ever sent to the configured origin.
            raise VideoError("人物アニメーションの結果URLを検証できません。", 502, "output_url")
        return url

    def _animate(self, face_png: bytes, narration_wav: bytes, deadline: float) -> bytes:
        base = self._prefix()
        inputs = [self._upload(base, "face.png", face_png, "image/png"),
                  self._upload(base, "narration.wav", narration_wav, "audio/wav"), *self.extra_args]
        try:
            started = self._request("POST", f"{self.origin}{base}/call{self.api_name}", json={"data": inputs}).json()
        except ValueError:
            started = None
        event_id = started.get("event_id") if isinstance(started, dict) else None
        if not isinstance(event_id, str) or not event_id.replace("-", "").isalnum():
            raise VideoError("人物アニメーションの実行先の応答を確認できませんでした。", 502, "lipsync_response")
        url = self._output_url(base, self._result(base, event_id, deadline))
        with self._request("GET", url, stream=True, timeout=(10, 120)) as response:
            return self._read_video(response, deadline)


def lipsync_from_env() -> LipSyncAdapter:
    provider = os.getenv("KARRY_LIPSYNC_PROVIDER", "").strip().lower()
    if provider in {"", "off", "none", "0"}:
        raise _config_error("人物アニメーションの実行先（SadTalker 等の GPU サーバー）が未設定です。")
    url, token = os.getenv("KARRY_LIPSYNC_URL", ""), os.getenv("KARRY_LIPSYNC_TOKEN", "").strip()
    if not url.strip():
        raise _config_error("人物アニメーションの実行先URL（KARRY_LIPSYNC_URL）が未設定です。")
    if provider == "http":
        return HttpLipSyncAdapter(url, token)
    if provider == "gradio":
        raw = os.getenv("KARRY_LIPSYNC_GRADIO_ARGS", "").strip()
        try:
            extra = json.loads(raw) if raw else None
        except ValueError:
            extra = "invalid"
        if extra is not None and not isinstance(extra, list):
            raise _config_error("KARRY_LIPSYNC_GRADIO_ARGS は JSON 配列で指定してください。")
        return GradioLipSyncAdapter(url, token, os.getenv("KARRY_LIPSYNC_GRADIO_API", DEFAULT_GRADIO_API).strip() or DEFAULT_GRADIO_API, extra)
    raise _config_error("KARRY_LIPSYNC_PROVIDER は http か gradio を指定してください。")
