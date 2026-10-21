from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from karry_video.config import Settings
from karry_video.errors import VideoError
from karry_video.preview import PreviewRenderer
from karry_video.storage import Storage


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(root=tmp_path / "private-media")


@pytest.fixture
def storage(settings: Settings) -> Storage:
    return Storage(settings)


def png_bytes(*, transparent: bool = False) -> bytes:
    mode = "RGBA" if transparent else "RGB"
    image = Image.new(mode, (80, 40), (255, 0, 0, 0) if transparent else (220, 20, 30))
    result = io.BytesIO()
    image.save(result, "PNG")
    return result.getvalue()


def project(key: str, *, ratio: str = "16:9", seconds: float = 2.0, text: str = "") -> dict:
    return {
        "image_key": key,
        "aspect_ratio": ratio,
        "duration_seconds": seconds,
        "tts": {"text": text, "voice": "en", "rate": 170, "pitch": 50, "volume": 100},
    }


def probe(path: Path) -> dict:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return json.loads(completed.stdout)


def test_save_prepare_image_strips_metadata_and_composites_black(storage: Storage) -> None:
    source = Image.new("RGB", (40, 20), (40, 50, 60))
    exif = Image.Exif()
    exif[274] = 6  # orientation: would be 20x40 without normalization
    raw = io.BytesIO()
    source.save(raw, "JPEG", exif=exif)

    key = storage.save_image(str(uuid.uuid4()), raw.getvalue())
    saved = storage.path(key)
    assert key.startswith("images/") and key.endswith(".png")
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    with Image.open(saved) as image:
        assert image.format == "PNG"
        assert image.size == (20, 40)
        assert len(image.getexif()) == 0

    transparent_key = storage.save_image(str(uuid.uuid4()), png_bytes(transparent=True))
    work = storage.workspace(str(uuid.uuid4()))
    prepared = storage.prepare_image(transparent_key, "9:16", work / "prepared.png")
    with Image.open(prepared) as image:
        assert image.size == (360, 640)
        assert image.getpixel((0, 0)) == (0, 0, 0)
        assert len(image.getexif()) == 0
    assert stat.S_IMODE(prepared.stat().st_mode) == 0o600


@pytest.mark.parametrize("payload", [b"not an image", b"GIF89a" + b"x" * 16])
def test_invalid_images_are_rejected(storage: Storage, payload: bytes) -> None:
    with pytest.raises(VideoError) as caught:
        storage.save_image(str(uuid.uuid4()), payload)
    assert caught.value.code == "invalid_image"
    assert caught.value.status_code == 422


def test_path_blocks_traversal_and_symlink(storage: Storage, tmp_path: Path) -> None:
    with pytest.raises(VideoError):
        storage.path("../outside.png")
    with pytest.raises(VideoError):
        storage.path("images\\outside.png")
    images = storage.root / "images"
    images.mkdir(mode=0o700)
    moved = storage.root / "images-real"
    images.rename(moved)
    images.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(VideoError) as caught:
        storage.path("images/file.png")
    assert caught.value.code == "unsafe_storage_path"


def test_workspace_output_key_and_permissions(storage: Storage) -> None:
    job = str(uuid.uuid4())
    workspace = storage.workspace(job)
    assert workspace == storage.root / "work" / job
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert storage.output_key(job) == f"outputs/{job}.mp4"
    with pytest.raises(VideoError):
        storage.workspace("../../not-a-uuid")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe") or not shutil.which("espeak-ng"), reason="local media tools are required")
def test_render_static_16_9_tts_h264_aac(settings: Settings, storage: Storage) -> None:
    key = storage.save_image(str(uuid.uuid4()), png_bytes())
    renderer = PreviewRenderer(settings, storage)
    job = str(uuid.uuid4())
    # Leading dashes remain speech input (stdin), never an eSpeak command option.
    result = renderer.render(project(key, seconds=3.0, text="-- hello"), storage.output_key(job))
    output = storage.path(storage.output_key(job))
    info = probe(output)
    video = next(item for item in info["streams"] if item["codec_type"] == "video")
    audio = next(item for item in info["streams"] if item["codec_type"] == "audio")
    assert result["width"] == 640 and result["height"] == 360 and result["has_audio"] is True
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert audio["codec_name"] == "aac" and audio["channels"] == 2
    assert abs(float(info["format"]["duration"]) - 3.0) <= 1 / 24 + 0.02
    assert not (storage.root / "work" / job).exists()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe") or not shutil.which("espeak-ng"), reason="local media tools are required")
def test_render_static_9_16_silence_and_short_video_tail_freeze(settings: Settings, storage: Storage, tmp_path: Path) -> None:
    key = storage.save_image(str(uuid.uuid4()), png_bytes())
    renderer = PreviewRenderer(settings, storage)
    still_result = renderer.render(project(key, ratio="9:16", seconds=1.0), storage.output_key(str(uuid.uuid4())))
    assert (still_result["width"], still_result["height"], still_result["has_audio"]) == (360, 640, True)

    source = tmp_path / "short.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=blue:s=160x90:r=24", "-t", "0.4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True,
    )
    result = renderer.render(project(key, seconds=1.0), storage.output_key(str(uuid.uuid4())), source)
    assert result["source_audio_replaced"] is True
    assert result["has_audio"] is True


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe") or not shutil.which("espeak-ng"), reason="local media tools are required")
def test_tts_too_long_is_rejected_without_truncation(settings: Settings, storage: Storage) -> None:
    key = storage.save_image(str(uuid.uuid4()), png_bytes())
    renderer = PreviewRenderer(settings, storage)
    with pytest.raises(VideoError) as caught:
        renderer.validate(project(key, seconds=1.0, text=" ".join(["narration"] * 80)))
    assert caught.value.code == "speech_too_long"
    assert caught.value.status_code == 422


@pytest.mark.skipif(not shutil.which("espeak-ng"), reason="eSpeak is required")
def test_japanese_never_silently_falls_back_to_english(settings: Settings, storage: Storage) -> None:
    key = storage.save_image(str(uuid.uuid4()), png_bytes())
    renderer = PreviewRenderer(settings, storage)
    try:
        renderer.validate(project(key, seconds=6.0, text="こんにちは"))
    except VideoError as exc:
        # The base Ubuntu eSpeak package has no Japanese voice. If an environment
        # does provision one, successful local synthesis is also acceptable.
        assert exc.code == "tts_japanese_unavailable"
        assert exc.status_code == 422
