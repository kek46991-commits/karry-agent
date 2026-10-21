import logging

import httpx
import pytest

from karry_video import Settings
from karry_video.providers import ReplicateKlingProvider


@pytest.mark.asyncio
async def test_signed_video_query_not_logged(tmp_path, caplog):
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32, headers={"Content-Type": "video/mp4"})
    provider = ReplicateKlingProvider(Settings(root=tmp_path), token="fake-token", transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO, logger="httpx"):
        await provider.download("https://replicate.delivery/private.mp4?signature=must-not-be-logged", tmp_path / "out.mp4")
    assert "must-not-be-logged" not in caplog.text
    assert "private.mp4" in caplog.text
