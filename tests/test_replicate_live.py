"""Live Replicate image-to-video check (paid, about $0.05 per run).

Skipped unless KARRY_LIVE_REPLICATE=1 and REPLICATE_API_TOKEN are set:
    KARRY_LIVE_REPLICATE=1 REPLICATE_API_TOKEN=... pytest tests/test_replicate_live.py
"""
import io
import os

import pytest
from PIL import Image, ImageDraw

from karry_agent import video_suite as vs

pytestmark = pytest.mark.skipif(
    os.getenv("KARRY_LIVE_REPLICATE") != "1" or not os.getenv("REPLICATE_API_TOKEN"),
    reason="live Replicate test is opt-in (KARRY_LIVE_REPLICATE=1 + REPLICATE_API_TOKEN)",
)


def sample_photo() -> bytes:
    image = Image.new("RGB", (832, 480), (70, 130, 200))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 330, 832, 480), fill=(30, 90, 160))
    draw.ellipse((560, 60, 680, 180), fill=(250, 220, 90))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_replicate_turns_one_photo_into_mp4():
    clip = vs.image_to_video(sample_photo(), "gentle ocean waves, sun shimmering", "16:9")
    assert vs.is_mp4(clip) and len(clip) > 10_000
