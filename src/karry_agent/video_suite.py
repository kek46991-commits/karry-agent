"""Creative Video Suite: image/text → video + Gemini TTS narration.

The default provider is ``local``: the server composes one MP4 (photo or title card,
slow pan, fades, narration audio) with a bundled ffmpeg, so no paid API is called.
Paid providers (Replicate Kling / Runway) are opt-in; they return clips of at most
10 s, so a 1–120 s request is planned as an ordered list of segments that the UI plays
in order and trims to the requested length.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import requests

from . import lipsync, task_persistence

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
        if response.status_code == 402:
            raise VideoError("動画生成サービスのクレジットが不足しています。プロバイダーの請求設定でクレジットを追加してください。", 502, "provider_credit")
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


LOCAL_SIZES = {"16:9": (1280, 720), "9:16": (720, 1280)}
LOCAL_FPS = 15
LOCAL_RENDER_TIMEOUT = 900
_FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
)
_RENDER_SLOT = threading.BoundedSemaphore(1)


def ffmpeg_exe() -> str | None:
    configured = os.getenv("KARRY_FFMPEG", "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _font(size: int):
    from PIL import ImageFont
    for path in [os.getenv("KARRY_VIDEO_FONT", ""), *_FONT_PATHS]:
        if path and os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size), True
            except OSError:
                continue
    return ImageFont.load_default(size=size), False


def _wrap(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines, line = [], ""
    for char in " ".join(text.split()):
        if draw.textlength(line + char, font=font) > width and line:
            lines.append(line)
            line = char.lstrip()
            if len(lines) == max_lines:
                lines[-1] = lines[-1][:-1] + "…"
                return lines
        else:
            line += char
    return lines + ([line] if line else [])


def compose_frame(aspect_ratio: str, prompt: str, image: bytes | None) -> bytes:
    """Base frame (12% larger than the output so ffmpeg can pan by cropping): photo cover-fit, else a title card."""
    from PIL import Image, ImageDraw, ImageOps
    width, height = LOCAL_SIZES[aspect_ratio]
    big = (round(width * 1.12 / 2) * 2, round(height * 1.12 / 2) * 2)
    if image:
        try:
            with Image.open(io.BytesIO(image)) as source:
                frame = ImageOps.fit(ImageOps.exif_transpose(source).convert("RGB"), big, Image.Resampling.LANCZOS)
        except Exception:
            raise VideoError("画像を読み込めませんでした。JPG / PNG / WEBP を選んでください。", 422, "invalid_image") from None
    else:
        top, bottom = (24, 38, 74), (190, 92, 48)
        gradient = Image.linear_gradient("L").resize(big)
        frame = Image.composite(Image.new("RGB", big, bottom), Image.new("RGB", big, top), gradient)
        draw = ImageDraw.Draw(frame)
        font, cjk = _font(max(28, big[0] // 18))
        text = prompt if cjk or prompt.isascii() else "KARRY VIDEO"
        lines = _wrap(draw, text, font, int(big[0] * 0.78), 6)
        line_height = int(font.size * 1.45)
        y = (big[1] - line_height * len(lines)) // 2
        for line in lines:
            x = (big[0] - draw.textlength(line, font=font)) // 2
            draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0))
            draw.text((x, y), line, font=font, fill=(255, 244, 228))
            y += line_height
    out = io.BytesIO()
    frame.save(out, "JPEG", quality=90)
    return out.getvalue()


class LocalVideoRenderer:
    """Free in-server composition: one MP4 per job, no external video API."""

    name = "local"
    local = True
    clip_lengths = (MAX_SECONDS,)

    def __init__(self, ffmpeg: str | None = None):
        self.ffmpeg = ffmpeg if ffmpeg is not None else ffmpeg_exe()
        if not self.ffmpeg:
            raise VideoError("動画の合成エンジン（ffmpeg）がサーバーにありません。", 503, "video_configuration")

    def render(self, prompt: str, aspect_ratio: str, seconds: int, image: bytes | None, narration: bytes | None,
               lip_sync: bool = False) -> bytes:
        """One MP4. With ``lip_sync`` the photo's mouth follows the narration loudness (raises LipSyncError if impossible)."""
        if aspect_ratio not in LOCAL_SIZES:
            raise VideoError("縦横比は 16:9 か 9:16 を選んでください。", 422, "invalid_aspect_ratio")
        plan_segments(seconds, self.clip_lengths)
        width, height = LOCAL_SIZES[aspect_ratio]
        fade = min(0.6, seconds / 4)
        video = (f"[0:v]crop={width}:{height}:x='(iw-ow)*t/{seconds}':y='(ih-oh)*t/{seconds}',"
                 f"fade=t=in:st=0:d={fade:.2f},fade=t=out:st={seconds - fade:.2f}:d={fade:.2f},format=yuv420p[v]")
        with tempfile.TemporaryDirectory(prefix="karry-video-") as work:
            frame, out = os.path.join(work, "frame.jpg"), os.path.join(work, "out.mp4")
            base = compose_frame(aspect_ratio, prompt, image)
            with open(frame, "wb") as handle:
                handle.write(base)
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            if lip_sync:
                if image is None:
                    raise lipsync.LipSyncError("no_photo")
                cmd += ["-f", "concat", "-safe", "0", "-i", self._lipsync_script(work, base, narration, seconds)]
            else:
                cmd += ["-loop", "1", "-framerate", str(LOCAL_FPS), "-i", frame]
            if narration:
                audio = os.path.join(work, "narration.wav")
                with open(audio, "wb") as handle:
                    handle.write(narration)
                cmd += ["-i", audio, "-filter_complex", video + ";[1:a]apad[a]", "-map", "[v]", "-map", "[a]",
                        "-c:a", "aac", "-b:a", "96k"]
            else:
                cmd += ["-filter_complex", video, "-map", "[v]"]
            cmd += ["-t", str(seconds), "-r", str(LOCAL_FPS), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                    "-movflags", "+faststart", out]
            try:
                with _RENDER_SLOT:
                    subprocess.run(cmd, check=True, capture_output=True, timeout=LOCAL_RENDER_TIMEOUT)
            except (subprocess.SubprocessError, OSError):
                raise VideoError("動画の合成に失敗しました。", 500, "render_failed") from None
            if os.path.getsize(out) > MAX_CLIP_BYTES:
                raise VideoError("合成した動画の容量上限を超えました。", 413, "output_capacity")
            with open(out, "rb") as handle:
                return handle.read()

    @staticmethod
    def _lipsync_script(work: str, base: bytes, narration: bytes | None, seconds: int) -> str:
        if not narration:
            raise lipsync.LipSyncError("no_narration")
        if not lipsync.available():
            raise lipsync.LipSyncError("unavailable")
        poses = lipsync.mouth_poses(base, lipsync.find_mouth(base))
        paths = []
        for index, pose in enumerate(poses):
            paths.append(os.path.join(work, f"mouth{index}.jpg"))
            with open(paths[-1], "wb") as handle:
                handle.write(pose)
        script = os.path.join(work, "frames.ffconcat")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(lipsync.concat_script(paths, lipsync.loudness_levels(narration, LOCAL_FPS, seconds * LOCAL_FPS), LOCAL_FPS))
        return script


    def render_talking(self, talking: bytes, prompt: str, aspect_ratio: str, seconds: int, image: bytes | None, narration: bytes) -> bytes:
        """Fit a lip-sync clip (usually a square face crop) onto a blurred photo background, then mux the narration."""
        if aspect_ratio not in LOCAL_SIZES:
            raise VideoError("縦横比は 16:9 か 9:16 を選んでください。", 422, "invalid_aspect_ratio")
        plan_segments(seconds, self.clip_lengths)
        from PIL import Image, ImageEnhance, ImageFilter
        width, height = LOCAL_SIZES[aspect_ratio]
        background = Image.open(io.BytesIO(compose_frame(aspect_ratio, prompt, image))).convert("RGB").resize((width, height))
        background = ImageEnhance.Brightness(background.filter(ImageFilter.GaussianBlur(24))).enhance(0.55)
        side = min(width, height)
        video = (f"[1:v]scale={side}:{side}:force_original_aspect_ratio=decrease,setsar=1,fps={LOCAL_FPS},"
                 f"tpad=stop_mode=clone:stop_duration={seconds}[fg];[0:v][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v];[2:a]apad[a]")
        with tempfile.TemporaryDirectory(prefix="karry-talking-") as work:
            frame, clip, audio, out = (os.path.join(work, n) for n in ("bg.jpg", "talking.mp4", "narration.wav", "out.mp4"))
            background.save(frame, "JPEG", quality=88)
            for path, body in ((clip, talking), (audio, narration)):
                with open(path, "wb") as handle:
                    handle.write(body)
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-loop", "1", "-framerate", str(LOCAL_FPS), "-i", frame, "-i", clip, "-i", audio,
                   "-filter_complex", video, "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "96k",
                   "-t", str(seconds), "-r", str(LOCAL_FPS), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                   "-movflags", "+faststart", out]
            try:
                with _RENDER_SLOT:
                    subprocess.run(cmd, check=True, capture_output=True, timeout=LOCAL_RENDER_TIMEOUT)
            except (subprocess.SubprocessError, OSError):
                raise VideoError("アニメーション動画の合成に失敗しました。", 500, "render_failed") from None
            if os.path.getsize(out) > MAX_CLIP_BYTES:
                raise VideoError("合成した動画の容量上限を超えました。", 413, "output_capacity")
            with open(out, "rb") as handle:
                return handle.read()


def normalize_lipsync_video(clip: bytes, narration: bytes, aspect_ratio: str, seconds: int) -> bytes:
    """外部の口パク映像を、元の音声・指定サイズ・指定秒数でMP4に整える。"""
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VideoError("口パク動画を整える合成エンジンがありません。", 503, "video_configuration")
    if aspect_ratio not in LOCAL_SIZES or not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise VideoError("口パク動画のサイズ・秒数が不正です。", 422, "invalid_duration")
    if len(clip) > MAX_CLIP_BYTES or b"ftyp" not in clip[:32]:
        raise VideoError("外部の口パク動画を検証できません。", 502, "sadtalker_output")
    width, height = LOCAL_SIZES[aspect_ratio]
    with tempfile.TemporaryDirectory(prefix="karry-lipsync-mux-") as work:
        source, audio, output = [os.path.join(work, name) for name in ("source.mp4", "voice.wav", "output.mp4")]
        for path, data in ((source, clip), (audio, narration)):
            with open(path, "wb") as handle:
                handle.write(data)
        video_filter = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                        f"tpad=stop_mode=clone:stop_duration={seconds},format=yuv420p")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1",
                   "-i", source, "-i", audio, "-vf", video_filter, "-af", "apad", "-map", "0:v:0", "-map", "1:a:0",
                   "-t", str(seconds), "-r", str(LOCAL_FPS), "-filter_threads", "1", "-c:v", "libx264",
                   "-preset", "ultrafast", "-crf", "28", "-threads", "1", "-c:a", "aac", "-b:a", "96k",
                   "-movflags", "+faststart", output]
        try:
            with _RENDER_SLOT:
                subprocess.run(command, check=True, capture_output=True, timeout=120)
            if os.path.getsize(output) > MAX_CLIP_BYTES:
                raise VideoError("口パク動画の容量上限を超えました。", 413, "output_capacity")
            with open(output, "rb") as handle:
                return handle.read()
        except (OSError, subprocess.SubprocessError):
            raise VideoError("口パク動画の合成に失敗しました。", 502, "sadtalker_output") from None


def adapter_from_env() -> VideoAdapter | LocalVideoRenderer:
    provider = os.getenv("KARRY_VIDEO_PROVIDER", "local").strip().lower() or "local"
    if provider == "local":
        return LocalVideoRenderer()
    if os.getenv("KARRY_VIDEO_GENERATION_ENABLED") != "1":
        raise VideoError("有料の動画生成APIは無効です。0円のローカル合成を使うには KARRY_VIDEO_PROVIDER=local にしてください。", 503, "generation_disabled")
    if provider == "replicate":
        return ReplicateVideoAdapter()
    if provider == "runway":
        return RunwayVideoAdapter()
    raise VideoError("KARRY_VIDEO_PROVIDER は local・replicate・runway のどれかを指定してください。", 503, "video_configuration")


class GeminiNarrator:
    """Gemini TTS via the documented Interactions REST API (returns WAV for unary requests)."""

    name = "gemini"

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


class FreeNarrator:
    """Key-free Japanese TTS (gTTS), converted to WAV with the bundled ffmpeg. Used when Gemini is unavailable."""

    name = "free"

    def __init__(self, ffmpeg: str | None = None, tts_factory: Callable[..., Any] | None = None):
        self.ffmpeg = ffmpeg if ffmpeg is not None else ffmpeg_exe()
        self.tts_factory = tts_factory
        if tts_factory is None and os.getenv("KARRY_FREE_TTS", "1") != "0":
            try:
                from gtts import gTTS
                self.tts_factory = gTTS
            except ImportError:
                self.tts_factory = None

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.tts_factory)

    def synthesize(self, text: str, voice: str) -> bytes:
        if not self.available:
            raise VideoError("無料の音声合成を使えません。", 503, "tts_configuration")
        try:
            mp3 = io.BytesIO()
            self.tts_factory(text, lang="ja", timeout=20).write_to_fp(mp3)
            with tempfile.TemporaryDirectory(prefix="karry-gtts-") as work:
                output = os.path.join(work, "speech.wav")
                subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "mp3", "-i", "pipe:0",
                                "-af", "atempo=1.2", "-ac", "1", "-ar", "24000", output],
                               input=mp3.getvalue(), capture_output=True, check=True, timeout=120)
                with open(output, "rb") as handle:
                    wav = handle.read(MAX_AUDIO_BYTES + 1)
            if not wav.startswith(b"RIFF") or len(wav) > MAX_AUDIO_BYTES:
                raise ValueError
            _validate_narration_wav(wav)
            return wav
        except Exception:
            raise VideoError("無料の音声合成サービスに接続できませんでした。", 502, "tts_failed") from None


class EspeakNarrator:
    """Offline Japanese speech synthesis for hosts that provide eSpeak NG."""

    name = "espeak-ng"

    def __init__(self, ffmpeg: str | None = None, executable: str | None = None):
        self.ffmpeg = ffmpeg if ffmpeg is not None else ffmpeg_exe()
        self.executable = executable if executable is not None else shutil.which("espeak-ng")

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg and self.executable)

    def synthesize(self, text: str, voice: str) -> bytes:
        if not self.available:
            raise VideoError("オフライン日本語音声を使えません。", 503, "tts_configuration")
        try:
            with tempfile.TemporaryDirectory(prefix="karry-espeak-") as work:
                source = os.path.join(work, "speech.wav")
                output = os.path.join(work, "normalized.wav")
                subprocess.run([self.executable, "-v", "ja", "-s", "170", "--stdin", "-w", source],
                               input=text.encode("utf-8"), capture_output=True, check=True, timeout=30)
                with open(source, "rb") as handle:
                    subprocess.run(
                        [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0",
                         "-af", "loudnorm=I=-18:TP=-2:LRA=7", "-ac", "1", "-ar", "24000", output],
                        input=handle.read(), capture_output=True, check=True, timeout=30,
                    )
                with open(output, "rb") as handle:
                    wav = handle.read(MAX_AUDIO_BYTES + 1)
            if len(wav) > MAX_AUDIO_BYTES:
                raise VideoError("オフライン音声の容量上限を超えました。", 413, "tts_invalid_audio")
            _validate_narration_wav(wav)
            return wav
        except VideoError:
            raise
        except Exception:
            raise VideoError("オフライン日本語音声を作成できませんでした。", 502, "tts_failed") from None


def _validate_narration_wav(wav: bytes) -> None:
    """Reject malformed, excessive, or effectively silent speech before muxing."""
    import struct
    import wave
    try:
        with wave.open(io.BytesIO(wav), "rb") as audio:
            frames = audio.readframes(audio.getnframes())
            sample_width, channels, rate = audio.getsampwidth(), audio.getnchannels(), audio.getframerate()
            if sample_width != 2 or channels not in (1, 2) or rate <= 0:
                raise ValueError
        duration = len(frames) / (sample_width * channels * rate)
        if not 0.05 <= duration <= 600:
            raise ValueError
        peak = max((abs(sample[0]) for sample in struct.iter_unpack("<h", frames[:len(frames) // 2 * 2])), default=0)
        if not peak or peak < 80:
            raise ValueError
    except Exception:
        raise VideoError("音声ファイルの形式または音量を検証できませんでした。", 502, "tts_invalid_audio") from None


def concatenate_mp4_clips(clips: list[bytes]) -> bytes:
    """Join compatible locally-rendered scene clips into one bounded MP4."""
    if not 1 <= len(clips) <= 8 or any(not isinstance(clip, bytes) or not clip or len(clip) > MAX_CLIP_BYTES
                                         or b"ftyp" not in clip[:32] for clip in clips):
        raise VideoError("シーン動画を検証できませんでした。", 502, "invalid_scene_video")
    if len(clips) == 1:
        return clips[0]
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VideoError("動画の結合エンジン（ffmpeg）がありません。", 503, "video_configuration")
    with tempfile.TemporaryDirectory(prefix="karry-autopilot-video-") as work:
        entries = []
        for index, clip in enumerate(clips):
            name = f"scene_{index:02d}.mp4"
            with open(os.path.join(work, name), "wb") as handle:
                handle.write(clip)
            entries.append(f"file '{name}'\n")
        with open(os.path.join(work, "scenes.ffconcat"), "w", encoding="utf-8") as handle:
            handle.writelines(entries)
        output = os.path.join(work, "final.mp4")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1",
                   "-i", "scenes.ffconcat", "-c", "copy", "-movflags", "+faststart", output]
        try:
            with _RENDER_SLOT:
                subprocess.run(command, check=True, capture_output=True, timeout=120, cwd=work)
            size = os.path.getsize(output)
            if not 0 < size <= MAX_CLIP_BYTES:
                raise VideoError("完成動画が容量上限を超えました。", 413, "output_capacity")
            with open(output, "rb") as handle:
                result = handle.read(MAX_CLIP_BYTES + 1)
        except VideoError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise VideoError("シーン動画を1本に結合できませんでした。", 502, "video_concat_failed") from None
    if b"ftyp" not in result[:32]:
        raise VideoError("結合後の動画を検証できませんでした。", 502, "invalid_scene_video")
    return result


def concatenate_wav_clips(clips: list[bytes]) -> bytes:
    """Join generated narration WAVs without exposing a path or FFmpeg diagnostic."""
    if not 1 <= len(clips) <= 8:
        raise VideoError("ナレーション音声の数が不正です。", 502, "tts_invalid_audio")
    for clip in clips:
        _validate_narration_wav(clip)
    if len(clips) == 1:
        return clips[0]
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VideoError("音声の結合エンジン（ffmpeg）がありません。", 503, "video_configuration")
    with tempfile.TemporaryDirectory(prefix="karry-autopilot-audio-") as work:
        entries = []
        for index, clip in enumerate(clips):
            name = f"voice_{index:02d}.wav"
            with open(os.path.join(work, name), "wb") as handle:
                handle.write(clip)
            entries.append(f"file '{name}'\n")
        with open(os.path.join(work, "voices.ffconcat"), "w", encoding="utf-8") as handle:
            handle.writelines(entries)
        output = os.path.join(work, "narration.wav")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1",
                   "-i", "voices.ffconcat", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", output]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=90, cwd=work)
            if not 0 < os.path.getsize(output) <= MAX_AUDIO_BYTES:
                raise VideoError("結合後の音声が容量上限を超えました。", 413, "tts_invalid_audio")
            with open(output, "rb") as handle:
                result = handle.read(MAX_AUDIO_BYTES + 1)
        except VideoError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise VideoError("ナレーション音声を結合できませんでした。", 502, "tts_invalid_audio") from None
    _validate_narration_wav(result)
    return result


class NarratorChain:
    """Tries each narrator in order (Gemini → free TTS) and remembers which one spoke."""

    def __init__(self, narrators: list[Any]):
        self.narrators = narrators
        self.last_engine: str | None = None

    @property
    def available(self) -> bool:
        return any(n.available for n in self.narrators)

    def synthesize(self, text: str, voice: str) -> bytes:
        error = VideoError("ナレーション音声のキーが未設定で、無料の音声合成も使えません。", 503, "tts_configuration")
        for narrator in self.narrators:
            if not narrator.available:
                continue
            try:
                wav = narrator.synthesize(text, voice)
                self.last_engine = narrator.name
                return wav
            except VideoError as exc:
                error = exc
        raise error


def narrator_from_env() -> NarratorChain:
    return NarratorChain([GeminiNarrator(), FreeNarrator(), EspeakNarrator()])


def placeholder_wav(rate: int = 24000) -> bytes:
    """Two soft beeps: an audible marker that narration could not be synthesized (never a silent 'success')."""
    import math
    import struct
    import wave
    beep, gap = int(rate * 0.18), int(rate * 0.12)
    tone = [int(6000 * math.sin(2 * math.pi * 880 * i / rate)) for i in range(beep)]
    samples = tone + [0] * gap + tone + [0] * int(rate * 0.5)
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return out.getvalue()


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
    narration, animation = job.get("narration"), job.get("animation")
    return {"mode": job.get("mode", "static"),
            "animation": None if not animation else {k: animation[k] for k in ("status", "engine", "error") if animation.get(k)},
            "id": job["id"], "request_id": job["request_id"], "status": job["status"], "prompt": job["prompt"],
            "aspect_ratio": job["aspect_ratio"], "duration_seconds": job["duration_seconds"], "provider": job["provider"],
            **({"lip_sync": {key: value for key, value in job["lip_sync"].items()
                             if key in {"requested", "status", "engine", "fallback_reason"}}} if job.get("lip_sync") else {}),
            "with_image": job["with_image"], "created_at": job["created_at"], "segments": segments,
            **({"autopilot": {key: value for key, value in job["autopilot"].items()
                               if key in {"status", "title", "scene_count", "current_scene", "fallback_used", "fallback_reason", "error"}}} if job.get("autopilot") else {}),
            "narration": None if not narration else {"text": narration["text"], "voice": narration["voice"], "status": narration["status"],
                                                     "muxed": bool(narration.get("muxed")), "engine": narration.get("engine"),
                                                     **({"error": narration["error"]} if narration.get("error") else {}),
                                                     **({"url": f"/api/video/jobs/{job['id']}/narration{q}"} if narration["status"] == "succeeded" else {})},
            "lipsync": job.get("lipsync")}


def overall_status(segments: list[dict[str, Any]]) -> str:
    states = {s["status"] for s in segments}
    if "processing" in states:
        return "processing"
    return "succeeded" if states == {"succeeded"} else ("failed" if states == {"failed"} else "partial")
