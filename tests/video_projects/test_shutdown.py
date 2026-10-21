import asyncio
import io
import threading
from uuid import uuid4

import pytest
from PIL import Image

from karry_video import Settings, VideoService
from karry_video.schemas import GenerateRequest, ProjectFields


@pytest.mark.asyncio
async def test_shutdown_waits_for_renderer_and_removes_uncommitted_output(tmp_path):
    started = threading.Event()
    release = threading.Event()
    class BlockingRenderer:
        storage = None
        def render(self, project, output_key, video_source=None):
            started.set()
            assert release.wait(timeout=5)
            self.storage.output_path(output_key).write_bytes(b"uncommitted-video")
            return {"duration_seconds": 5, "width": 360, "height": 640, "has_audio": True}
    renderer = BlockingRenderer()
    settings = Settings(root=tmp_path / "private", database_url=f"sqlite:///{tmp_path / 'projects.sqlite'}", api_token="test-only")
    service = VideoService(settings, renderer=renderer)
    renderer.storage = service.storage
    await service.start()
    image = io.BytesIO()
    Image.new("RGB", (32, 32), "black").save(image, format="PNG")
    project_id = str(uuid4())
    key = service.storage.save_image(project_id, image.getvalue())
    service.repository.create_project(project_id, ProjectFields(title="Shutdown test").model_dump(), key)
    job = await service.generate(GenerateRequest(project_id=project_id), "preview")
    assert await asyncio.to_thread(started.wait, 5)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0.05)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, timeout=5)
    assert not list((settings.root / "outputs").glob("*.mp4"))
    assert service.repository.get_job(job["id"])["status"] == "interrupted"
    service.database.close()
