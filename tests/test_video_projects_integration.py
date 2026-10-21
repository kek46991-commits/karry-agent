import io
import json
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as web
from karry_video import Settings

TOKEN = "synthetic-video-owner-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def image():
    stream = io.BytesIO()
    Image.new("RGB", (64, 100), "navy").save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_VIDEO_PROJECTS_ENABLED", "1")
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)
    monkeypatch.setenv("VIDEO_API_TOKEN", "must-not-bypass-owner")
    settings = Settings(root=tmp_path / "private", database_url=f"sqlite:///{tmp_path / 'video.sqlite'}", api_token=TOKEN)
    monkeypatch.setattr(web.app.state.video_projects, "settings_factory", lambda: settings)
    for key in ("REPLICATE_API_TOKEN", "RUNWAY_API_KEY", "KLING_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with TestClient(web.app) as client:
        yield client, settings


def create(client, **data):
    return client.post("/api/video/projects", data={"title": "統合プレビュー", "duration_seconds": "3", **data}, files={"image": ("image.png", image(), "image/png")}, headers=AUTH)


def test_project_api_registered_without_legacy_collision(configured):
    client, _ = configured
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/api/video/projects", "/api/video/generate/preview", "/api/video/generate/render", "/api/video/project-jobs/{job_id}", "/api/video/jobs/{job_id}"):
        assert path in paths
    routes = [(route.path, method) for route in web.app.routes for method in getattr(route, "methods", [])]
    assert len(routes) == len(set(routes))
    session = client.post("/api/sessions", json={"title": "動画旧API共存"}, headers=AUTH).json()["session_id"]
    assert client.get("/api/video/jobs", params={"session_id": session}, headers=AUTH).status_code == 200
    assert client.get("/api/video/project-config", headers=AUTH).json()["preview_available"]


def test_owner_auth_and_dynamic_rotation(configured, monkeypatch):
    client, _ = configured
    assert client.get("/api/video/projects").status_code == 401
    assert client.get("/api/video/projects", headers={"Authorization": "Bearer must-not-bypass-owner"}).status_code == 401
    monkeypatch.setenv("KARRY_API_TOKEN", "rotated-owner-token")
    assert client.get("/api/video/projects", headers=AUTH).status_code == 401
    assert client.get("/api/video/projects", headers={"Authorization": "Bearer rotated-owner-token"}).status_code == 200
    monkeypatch.delenv("KARRY_API_TOKEN")
    assert client.get("/api/video/projects", headers=AUTH).status_code == 503


def test_create_patch_list_delete_and_private_image(configured):
    client, settings = configured
    response = create(client)
    assert response.status_code == 201, response.text
    project = response.json()
    assert str(settings.root) not in json.dumps(project)
    assert client.get(project["image_url"], headers=AUTH).status_code == 200
    assert client.get(project["image_url"]).status_code == 401
    updated = client.patch(f"/api/video/projects/{project['project_id']}", json={"version": project["version"], "title": "更新済み"}, headers=AUTH)
    assert updated.status_code == 200
    assert client.get("/api/video/projects", headers=AUTH).json()["total"] == 1
    assert client.delete(f"/api/video/projects/{project['project_id']}?version={updated.json()['version']}", headers=AUTH).status_code == 204


def test_real_preview_via_actual_app(configured):
    client, _ = configured
    project = create(client, tts=json.dumps({"text": "Hello world.", "voice": "en"})).json()
    response = client.post("/api/video/generate/preview", json={"project_id": project["project_id"], "request_id": str(uuid4())}, headers=AUTH)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status_url"].startswith("/api/video/project-jobs/")
    deadline = time.monotonic() + 30
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.05)
        job = client.get(job["status_url"], headers=AUTH).json()
    assert job["status"] == "succeeded", job
    assert job["output_metadata"]["width"] == 360
    assert job["output_metadata"]["height"] == 640
    assert job["download_url"].startswith("/api/video/project-jobs/")
    assert client.get(job["download_url"]).status_code == 401
    data = client.get(job["download_url"], headers=AUTH).content
    assert b"ftyp" in data[:32]
    assert client.get(f"/api/video/projects/{project['project_id']}/download", headers=AUTH).content == data


def test_render_not_configured_is_503(configured):
    client, _ = configured
    project = create(client).json()
    response = client.post("/api/video/generate/render", json={"project_id": project["project_id"]}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "provider_not_configured"


def test_invalid_db_does_not_break_chat_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)
    monkeypatch.setenv("KARRY_VIDEO_PROJECTS_ENABLED", "1")
    monkeypatch.setattr(web.app.state.video_projects, "settings_factory", lambda: Settings(root=tmp_path, database_url="mysql://localhost/unsupported", api_token=TOKEN))
    with TestClient(web.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/video/project-config", headers=AUTH).json()["enabled"] is False
        assert client.get("/api/video/projects", headers=AUTH).status_code == 503


def test_secret_lifespan_preserved_and_service_closed(configured):
    _, _ = configured
    assert any(worker.name == "secret-chat-expiry" for worker in threading.enumerate())
    assert web.app.state.video_projects.service is not None


def test_disabled_keeps_existing_health_and_video_api(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)
    with TestClient(web.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/video/projects", headers=AUTH).status_code == 503
        session = client.post("/api/sessions", json={"title": "既存API"}, headers=AUTH).json()["session_id"]
        assert client.get("/api/video/jobs", params={"session_id": session}, headers=AUTH).status_code == 200


def test_silent_preview_without_espeak(tmp_path):
    from karry_video.preview import PreviewRenderer
    from karry_video.storage import Storage
    settings = Settings(root=tmp_path, espeak_bin="deliberately-missing-espeak")
    storage = Storage(settings)
    key = storage.save_image(str(uuid4()), image())
    project = {"image_key": key, "aspect_ratio": "16:9", "duration_seconds": 1, "tts": {"text": "", "voice": "en"}}
    renderer = PreviewRenderer(settings, storage)
    renderer.validate(project)
    result = renderer.render(project, storage.output_key(str(uuid4())))
    assert result["has_audio"] and abs(result["duration_seconds"] - 1) < 0.1
