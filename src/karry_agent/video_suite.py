"""Creative Video Suite: image/text → video clips (Replicate Kling or Runway) + Gemini TTS narration.

Providers return clips of at most 10 s, so a 1–120 s request is planned as an ordered
list of segments. Each segment is a separate provider job; the UI plays them in order
and trims the last one to the requested length.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import requests

from . import task_persistence

ASPECT_RATIOS = ("16:9", "9:16")
MIN_SECONDS, MAX_SECONDS = 1, 120
MAX_CLIP_BYTES = 60 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_JOBS_PER_SESSION = 20
MAX_DATA_URI_BYTES = 900 * 1024
VOICES = {"jimmy": "Charon", "fenrir": "Fenrir", "puck": "Puck", "orus": "Orus", "kore": "Kore"}
VOICE_LABELS = {"jimmy": "ジミー（低めの男性）", "fenrir": "元気な男性", "puck": "明るい男性", "orus": "落ち着いた男性", "kore": "女性"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


class VideoError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = "video_error"):
        super().__init__(message)
        self.message, self.status_code, self.code = message, status_code, code


@dataclass
class Submission:
    provider_id: str
    status: str  # processing | succeeded | failed
    output_url: str | None = None
    error: str | None = None


class VideoAdapter(Protocol):
    name: str
    clip_lengths: tuple[int, ...]

    def submit(self, prompt: str, aspect_ratio: str, seconds: int, image_data_uri: str | None) -> Submission: ...

    def poll(self, provider_id: str) -> Submission: ...

    def download(self, url: str) -> bytes: ...


def plan_segments(duration: int, clip_lengths: tuple[int, ...]) -> list[dict[str, int]]:
    """Split ``duration`` seconds into provider clips; ``play`` is how long each clip is shown."""
    if not MIN_SECONDS <= duration <= MAX_SECONDS:
        raise VideoError(f"長さは{MIN_SECONDS}〜{MAX_SECONDS}秒で指定してください。", 422, "invalid_duration")
    longest = max(clip_lengths)
    segments, remaining = [], duration
    while remaining > 0:
        clip = min((n for n in clip_lengths if n >= min(remaining, longest)), default=longest)
        segments.append({"index": len(segments), "clip_seconds": clip, "play_seconds": min(remaining, clip)})
        remaining -= min(remaining, clip)
    return segments


def image_data_uri(body: bytes) -> str:
    """Re-encode an uploaded image as a JPEG data URI small enough for provider inputs (<1 MB)."""
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(body)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except Exception:
        raise VideoError("画像を読み込めませんでした。JPG / PNG / WEBP を選んでください。", 422, "invalid_image") from None
    for side, quality in ((1280, 85), (1280, 70), (1024, 70), (768, 65), (512, 60)):
        copy_ = image.copy()
        copy_.thumbnail((side, side))
        out = io.BytesIO()
        copy_.save(out, "JPEG", quality=quality)
        if out.tell() <= MAX_DATA_URI_BYTES:
            return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()
    raise VideoError("画像を十分に小さくできませんでした。", 413, "image_too_large")


def _download(url: str, allowed_hosts: tuple[str, ...], limit: int = MAX_CLIP_BYTES) -> bytes:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise VideoError("生成動画の保存先URLを検証できません。", 502, "output_url")
    body = bytearray()
    deadline = time.monotonic() + 60
    try:
        with requests.Session() as session:
            session.trust_env = False
            # Provider credentials are never sent to the file origin.
            with session.get(url, timeout=(5, 30), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise VideoError("生成動画を取得できません。少し待って再確認してください。", 502, "output_download")
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise VideoError("生成動画の容量上限を超えました。", 413, "output_capacity")
                    if time.monotonic() > deadline:
                        raise VideoError("生成動画の取得期限を超えました。", 502, "output_download")
    except requests.RequestException:
        raise VideoError("生成動画の取得に失敗しました。", 502, "output_download") from None
    return bytes(body)


class _HttpAdapter:
    name = ""
    output_hosts: tuple[str, ...] = ()

    def __init__(self, token: str, session: Any | None = None):
        if not token or any(ch in token for ch in "\r\n"):
            raise VideoError(f"{self.name} のAPIキーがサーバーに設定されていません。", 503, "video_configuration")
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            self.session.trust_env = False
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}

    def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(method, url, headers={**self.headers, **kwargs.pop("headers", {})},
                                            timeout=(5, 30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise VideoError("動画生成APIに接続できません。", 502, "provider_unavailable") from None
        if response.status_code in {400, 401, 403, 404, 422, 429}:
            raise VideoError("動画生成の受付が拒否されました。キー・入力・利用枠を確認してください。", 502, "submission_rejected")
        try:
            if response.status_code not in {200, 201}:
                raise ValueError
            data = response.json()
            if not isinstance(data, dict) or not _ID.fullmatch(str(data.get("id", ""))):
                raise ValueError
            return data
        except ValueError:
            raise VideoError("動画生成APIの応答を確認できませんでした。", 502, "provider_response") from None

    def download(self, url: str) -> bytes:
        return _download(url, self.output_hosts)


class ReplicateVideoAdapter(_HttpAdapter):
    """Kling on Replicate (``kwaivgi/kling-v2.5-turbo-pro``: prompt, start_image, aspect_ratio, duration 5|10)."""

    name = "replicate"
    clip_lengths = (5, 10)
    output_hosts = ("replicate.delivery",)
    api = "https://api.replicate.com/v1"

    def __init__(self, token: str | None = None, model: str | None = None, session: Any | None = None):
        super().__init__(token if token is not None else os.getenv("REPLICATE_API_TOKEN", ""), session)
        self.model = model or os.getenv("KARRY_VIDEO_REPLICATE_MODEL", "kwaivgi/kling-v2.5-turbo-pro")

    @staticmethod
    def _submission(data: dict[str, Any]) -> Submission:
        status = data.get("status")
        if status == "succeeded":
            output = data.get("output")
            url = output[0] if isinstance(output, list) and output else output
            return Submission(data["id"], "succeeded", url if isinstance(url, str) else None)
        if status in {"failed", "canceled"}:
            return Submission(data["id"], "failed", error="prediction_" + status)
        return Submission(data["id"], "processing")

    def submit(self, prompt, aspect_ratio, seconds, image_data_uri):
        payload: dict[str, Any] = {"prompt": prompt, "duration": seconds, "aspect_ratio": aspect_ratio}
        if image_data_uri:
            payload["start_image"] = image_data_uri
        return self._submission(self._call("POST", f"{self.api}/models/{self.model}/predictions",
                                           json={"input": payload}, headers={"Cancel-After": "15m"}))

    def poll(self, provider_id):
        if not _ID.fullmatch(provider_id):
            raise VideoError("保存された生成IDが不正です。", 500, "job_integrity")
        return self._submission(self._call("GET", f"{self.api}/predictions/{provider_id}"))


class RunwayVideoAdapter(_HttpAdapter):
    """Runway API: ``image_to_video`` (gen4_turbo) with a photo, ``text_to_video`` (gen4.5) without."""

    name = "runway"
    clip_lengths = tuple(range(2, 11))
    api = "https://api.dev.runwayml.com/v1"
    ratios = {"16:9": "1280:720", "9:16": "720:1280"}

    def __init__(self, token: str | None = None, session: Any | None = None):
        super().__init__(token if token is not None else os.getenv("RUNWAYML_API_SECRET", ""), session)
        self.headers["X-Runway-Version"] = "2024-11-06"
        hosts = os.getenv("KARRY_VIDEO_OUTPUT_HOSTS", "cloudfront.net,runwayml.com")
        self.output_hosts = tuple(h.strip().lower() for h in hosts.split(",") if h.strip())

    @staticmethod
    def _submission(data: dict[str, Any]) -> Submission:
        status = data.get("status")
        if status == "SUCCEEDED":
            output = data.get("output")
            return Submission(data["id"], "succeeded", output[0] if isinstance(output, list) and output else None)
        if status in {"FAILED", "CANCELLED"}:
            return Submission(data["id"], "failed", error=str(data.get("failureCode") or "task_" + status.lower())[:80])
        return Submission(data["id"], "processing")

    def submit(self, prompt, aspect_ratio, seconds, image_data_uri):
        body: dict[str, Any] = {"promptText": prompt[:1000], "ratio": self.ratios[aspect_ratio], "duration": seconds}
        if image_data_uri:
            body |= {"model": "gen4_turbo", "promptImage": image_data_uri}
            data = self._call("POST", f"{self.api}/image_to_video", json=body)
        else:
            data = self._call("POST", f"{self.api}/text_to_video", json=body | {"model": "gen4.5"})
        return Submission(data["id"], "processing")

    def poll(self, provider_id):
        try:
            uuid.UUID(provider_id)
        except ValueError:
            raise VideoError("保存された生成IDが不正です。", 500, "job_integrity") from None
        return self._submission(self._call("GET", f"{self.api}/tasks/{provider_id}"))


def adapter_from_env() -> VideoAdapter:
    if os.getenv("KARRY_VIDEO_GENERATION_ENABLED") != "1":
        raise VideoError("動画生成は未有効化です。管理者がキーと利用設定を確認してください。", 503, "generation_disabled")
    provider = os.getenv("KARRY_VIDEO_PROVIDER", "replicate").strip().lower()
    if provider == "replicate":
        return ReplicateVideoAdapter()
    if provider == "runway":
        return RunwayVideoAdapter()
    raise VideoError("KARRY_VIDEO_PROVIDER は replicate か runway を指定してください。", 503, "video_configuration")


class GeminiNarrator:
    """Gemini TTS via the documented Interactions REST API (returns WAV for unary requests)."""

    url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    def __init__(self, api_key: str | None = None, model: str | None = None, session: Any | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("KARRY_TTS_MODEL", "gemini-3.8-flash-tts")
        self.session = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, text: str, voice: str) -> bytes:
        if not self.available:
            raise VideoError("ナレーション音声のキーが未設定です。", 503, "tts_configuration")
        body = {"model": self.model, "response_format": {"type": "audio"},
                "generation_config": {"speech_config": [{"voice": VOICES.get(voice, VOICES["jimmy"])}]},
                "input": [{"type": "user_input", "content": [{"type": "text", "text": text, "annotations": [
                    {"type": "speech_metadata", "style": "落ち着いた低めの男性の声で、テンポよく自然に"}]}]}]}
        try:
            response = self.session.post(self.url, json=body, headers={"x-goog-api-key": self.api_key}, timeout=(5, 60))
            if response.status_code != 200:
                raise ValueError
            audio = [part for step in response.json().get("steps", []) if step.get("type") == "model_output"
                     for part in step.get("content", []) if part.get("type") == "audio"]
            wav = base64.b64decode(audio[-1]["data"])
            if not wav.startswith(b"RIFF") or len(wav) > MAX_AUDIO_BYTES:
                raise ValueError
            return wav
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            raise VideoError("ナレーション音声を作れませんでした。", 502, "tts_failed") from None


_MEMORY_DOCS: dict[str, dict[str, Any]] = {}
_MEMORY_BLOBS: dict[str, bytes] = {}
_MEMORY_LOCK = threading.Lock()


class VideoJobStore:
    """One JSON document per task (jobs) plus private binary blobs (clips, narration)."""

    def __init__(self, session_id: str):
        self.durable = task_persistence.mode() == "s3"
        self.repo = task_persistence.repository() if self.durable else None
        self.base = ("" if self.repo is None else self.repo.prefix) + "videos/" + hashlib.sha256(session_id.encode()).hexdigest()

    def jobs(self) -> dict[str, dict[str, Any]]:
        if not self.durable:
            with _MEMORY_LOCK:
                return copy.deepcopy(_MEMORY_DOCS.get(self.base, {}).get("jobs", {}))
        value, _ = self.repo._read(self.base + ".json")
        return (value or {}).get("jobs", {})

    def save(self, job: dict[str, Any]) -> None:
        def apply(current: dict[str, Any]) -> dict[str, Any]:
            jobs = current.setdefault("jobs", {})
            jobs[job["id"]] = job
            for old in sorted(jobs.values(), key=lambda j: j["created_at"])[:-MAX_JOBS_PER_SESSION]:
                jobs.pop(old["id"], None)
            return current
        if not self.durable:
            with _MEMORY_LOCK:
                _MEMORY_DOCS[self.base] = apply(copy.deepcopy(_MEMORY_DOCS.get(self.base, {"version": 1})))
            return
        self.repo._cas(self.base + ".json", {"version": 1, "jobs": {}}, apply)

    def put_blob(self, name: str, body: bytes, mime: str) -> None:
        key = f"{self.base}/{name}"
        if not self.durable:
            with _MEMORY_LOCK:
                _MEMORY_BLOBS[key] = body
            return
        self.repo.client.put_object(Bucket=self.repo.config.bucket, Key=key, Body=body, ContentType=mime,
                                    CacheControl="no-store", Metadata={"sha256": hashlib.sha256(body).hexdigest()})

    def get_blob(self, name: str) -> bytes:
        key = f"{self.base}/{name}"
        if not self.durable:
            with _MEMORY_LOCK:
                if key not in _MEMORY_BLOBS:
                    raise VideoError("ファイルが見つかりません。", 404, "not_found")
                return _MEMORY_BLOBS[key]
        try:
            response = self.repo.client.get_object(Bucket=self.repo.config.bucket, Key=key)
            with response["Body"] as body:
                return body.read(MAX_CLIP_BYTES + 1)
        except Exception:
            raise VideoError("ファイルが見つかりません。", 404, "not_found") from None


def public_job(job: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Job view for the UI: no provider IDs or provider URLs."""
    from urllib.parse import quote
    q = "?session_id=" + quote(session_id)
    segments = [{"index": s["index"], "status": s["status"], "clip_seconds": s["clip_seconds"], "play_seconds": s["play_seconds"],
                 **({"url": f"/api/video/jobs/{job['id']}/clips/{s['index']}{q}"} if s["status"] == "succeeded" else {})}
                for s in job["segments"]]
    narration = job.get("narration")
    return {"id": job["id"], "request_id": job["request_id"], "status": job["status"], "prompt": job["prompt"],
            "aspect_ratio": job["aspect_ratio"], "duration_seconds": job["duration_seconds"], "provider": job["provider"],
            "with_image": job["with_image"], "created_at": job["created_at"], "segments": segments,
            "narration": None if not narration else {"text": narration["text"], "voice": narration["voice"], "status": narration["status"],
                                                     **({"url": f"/api/video/jobs/{job['id']}/narration{q}"} if narration["status"] == "succeeded" else {})}}


def overall_status(segments: list[dict[str, Any]]) -> str:
    states = {s["status"] for s in segments}
    if "processing" in states:
        return "processing"
    return "succeeded" if states == {"succeeded"} else ("failed" if states == {"failed"} else "partial")
