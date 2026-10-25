import io
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from PIL import Image

from karry_video import Settings, create_app
from karry_video.errors import SubmissionUnknown

TOKEN = "unit-test-video-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (80, 100), "navy").save(buffer, format="PNG")
    return buffer.getvalue()


def make_project(client, **fields):
    data = {"title": "動画テスト", "duration_seconds": "5", **fields}
    return client.post("/api/video/projects", data=data, files={"image": ("photo.png", image_bytes(), "image/png")}, headers=HEADERS)


def wait_job(client, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/video/jobs/{job_id}", headers=HEADERS)
        assert response.status_code == 200
        job = response.json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("Job did not finish")


class FakeRenderer:
    def __init__(self, delay=0):
        self.delay = delay
        self.storage = None
    def validate(self, project):
        pass
    def render(self, project, output_key, video_source=None):
        time.sleep(self.delay)
        path = self.storage.path(output_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-mp4-output")
        return {"duration_seconds": project["duration_seconds"], "width": 360, "height": 640, "has_audio": True}


class FakeProvider:
    name = "replicate"
    def __init__(self, mode="success"):
        self.mode = mode
        self.starts = 0
        self.polls = 0
    def validate(self, project):
        pass
    async def start(self, project, image_path):
        self.starts += 1
        if self.mode == "unknown":
            raise SubmissionUnknown()
        return "accepted-safe-id"
    async def poll(self, accepted):
        self.polls += 1
        if self.mode == "waiting":
            return SimpleNamespace(status="running", output_url=None)
        return SimpleNamespace(status="succeeded", output_url="https://replicate.delivery/fake.mp4")
    async def download(self, url, destination):
        destination.write_bytes(b"source-video")


@pytest.fixture
def settings(tmp_path):
    return Settings(root=tmp_path / "private", database_url=f"sqlite:///{tmp_path / 'projects.sqlite'}", api_token=TOKEN, poll_timeout=1, poll_interval=0.01)


@pytest.fixture
def client(settings):
    renderer = FakeRenderer()
    app = create_app(settings, renderer=renderer)
    renderer.storage = app.state.video_service.storage
    with TestClient(app) as client:
        yield client


def test_crud_auth_and_no_path_leak(client, settings):
    assert client.get("/api/video/projects").status_code == 401
    project = make_project(client).json()
    assert "project_id" in project and "image_key" not in project
    assert str(settings.root) not in json.dumps(project)
    assert client.get("/api/video/projects", headers=HEADERS).json()["total"] == 1
    assert client.get(project["image_url"]).status_code == 401
    assert client.get(project["image_url"], headers=HEADERS).headers["content-type"] == "image/png"
    response = client.patch(f"/api/video/projects/{project['project_id']}", json={"version": project["version"], "title": "改訂"}, headers=HEADERS)
    assert response.status_code == 200
    assert client.patch(f"/api/video/projects/{project['project_id']}", json={"version": 1, "title": "古い更新"}, headers=HEADERS).status_code == 409
    version = response.json()["version"]
    assert client.delete(f"/api/video/projects/{project['project_id']}?version={version}", headers=HEADERS).status_code == 204
    assert client.get(project["image_url"], headers=HEADERS).status_code == 404


@pytest.mark.parametrize("fields", [{"title": " "}, {"aspect_ratio": "1:1"}, {"duration_seconds": "0"}, {"duration_seconds": "nan"}, {"tts": "bad-json"}, {"tts": '{"voice":"--help"}'}, {"tts": '{"unexpected":"value"}'}])
def test_invalid_fields(client, fields):
    assert make_project(client, **fields).status_code == 422
    assert client.get("/api/video/projects", headers=HEADERS).json()["total"] == 0


def test_bad_image_and_body_limit(client):
    response = client.post("/api/video/projects", data={"title": "fake"}, files={"image": ("bad.png", b"not-image", "image/png")}, headers=HEADERS)
    assert response.status_code == 422
    huge = {**HEADERS, "Content-Length": "999999999"}
    assert client.post("/api/video/projects", content=b"x", headers=huge).status_code == 413
    assert client.post("/api/video/projects", content=b"x", headers={"Content-Length": "999999999"}).status_code == 401


def test_preview_is_local_even_with_provider_credentials(client, monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "not-a-real-provider-key")
    project = make_project(client).json()
    request = {"project_id": project["project_id"], "request_id": str(uuid4())}
    response = client.post("/api/video/generate/preview", json=request, headers=HEADERS)
    assert response.status_code == 202
    job = wait_job(client, response.json()["job_id"])
    assert job["status"] == "succeeded" and job["provider"] is None
    assert client.post("/api/video/generate/preview", json=request, headers=HEADERS).json()["job_id"] == job["job_id"]
    assert client.get(job["download_url"]).status_code == 401
    download = client.get(job["download_url"], headers=HEADERS)
    assert download.content == b"fake-mp4-output"
    assert "no-store" in download.headers["cache-control"]


def test_unconfigured_render_503(client, monkeypatch):
    for key in ("REPLICATE_API_TOKEN", "RUNWAY_API_KEY", "KLING_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    project = make_project(client).json()
    response = client.post("/api/video/generate/render", json={"project_id": project["project_id"], "request_id": str(uuid4())}, headers=HEADERS)
    assert response.status_code == 503
    assert client.get(f"/api/video/projects/{project['project_id']}", headers=HEADERS).json()["status"] == "draft"


def test_duplicate_render_starts_only_once(settings):
    provider, renderer = FakeProvider(), FakeRenderer()
    app = create_app(settings, provider_factory=lambda *_: provider, renderer=renderer)
    renderer.storage = app.state.video_service.storage
    with TestClient(app) as client:
        project = make_project(client).json()
        request = {"project_id": project["project_id"], "request_id": str(uuid4())}
        first = client.post("/api/video/generate/render", json=request, headers=HEADERS).json()
        second = client.post("/api/video/generate/render", json=request, headers=HEADERS).json()
        assert first["job_id"] == second["job_id"]
        job = wait_job(client, first["job_id"])
        assert job["status"] == "succeeded"
        assert provider.starts == 1
        receipt = app.state.video_service._read_receipt(app.state.video_service.repository.get_job(first["job_id"]))
        assert receipt == "accepted-safe-id"


def test_unknown_submission_blocks_retry(settings):
    provider, renderer = FakeProvider("unknown"), FakeRenderer()
    app = create_app(settings, provider_factory=lambda *_: provider, renderer=renderer)
    renderer.storage = app.state.video_service.storage
    with TestClient(app) as client:
        project = make_project(client).json()
        first = client.post("/api/video/generate/render", json={"project_id": project["project_id"], "request_id": str(uuid4())}, headers=HEADERS).json()
        job = wait_job(client, first["job_id"])
        assert job["status"] == "awaiting_resume" and job["error_code"] == "submission_unknown"
        assert client.post(f"/api/video/jobs/{job['job_id']}/resume", headers=HEADERS).status_code == 409
        assert client.post("/api/video/generate/render", json={"project_id": project["project_id"], "request_id": str(uuid4())}, headers=HEADERS).status_code == 409
        assert provider.starts == 1
        assert client.post(f"/api/video/jobs/{job['job_id']}/abandon", json={}, headers=HEADERS).status_code == 422
        assert client.post(f"/api/video/jobs/{job['job_id']}/abandon", json={"acknowledge_unknown": True}, headers=HEADERS).status_code == 200


def test_resume_uses_accepted_id_no_second_start(settings):
    provider, renderer = FakeProvider("waiting"), FakeRenderer()
    app = create_app(settings, provider_factory=lambda *_: provider, renderer=renderer)
    renderer.storage = app.state.video_service.storage
    with TestClient(app) as client:
        project = make_project(client).json()
        first = client.post("/api/video/generate/render", json={"project_id": project["project_id"], "request_id": str(uuid4())}, headers=HEADERS).json()
        job = wait_job(client, first["job_id"])
        assert job["status"] == "awaiting_resume" and job["error_code"] == "poll_timeout"
        provider.mode = "success"
        assert client.post(f"/api/video/jobs/{job['job_id']}/resume", headers=HEADERS).status_code == 202
        assert wait_job(client, job["job_id"])["status"] == "succeeded"
        assert provider.starts == 1


def test_missing_token_is_fail_closed(settings):
    from dataclasses import replace
    with TestClient(create_app(replace(settings, api_token=""))) as client:
        assert client.get("/api/video/projects").status_code == 503


def test_custom_auth_dependency(settings):
    from dataclasses import replace
    from karry_video.errors import VideoError
    async def auth(request: Request):
        if request.headers.get("x-session-test") != "ok":
            raise VideoError("unauthorized", "認証が必要です。", 401)
    with TestClient(create_app(replace(settings, api_token=""), auth_dependency=auth)) as client:
        assert client.get("/api/video/projects").status_code == 401
        assert client.get("/api/video/projects", headers={"X-Session-Test": "ok"}).status_code == 200


def test_real_ffmpeg_preview(settings):
    with TestClient(create_app(settings)) as client:
        project = make_project(client, aspect_ratio="16:9", duration_seconds="3", tts=json.dumps({"text": "Hello world.", "voice": "en"})).json()
        response = client.post("/api/video/generate/preview", json={"project_id": project["project_id"], "request_id": str(uuid4())}, headers=HEADERS)
        assert response.status_code == 202
        job = wait_job(client, response.json()["job_id"], timeout=30)
        assert job["status"] == "succeeded", job
        metadata = job["output_metadata"]
        assert metadata["width"] == 640 and metadata["height"] == 360
        assert abs(metadata["duration_seconds"] - 3) < 0.1
        data = client.get(job["download_url"], headers=HEADERS).content
        assert b"ftyp" in data[:32]


def test_request_id_can_be_omitted(client):
    project = make_project(client).json()
    response = client.post('/api/video/generate/preview', json={'project_id': project['project_id']}, headers=HEADERS)
    assert response.status_code == 202
    assert response.json()['request_id']
    assert wait_job(client, response.json()['job_id'])['status'] == 'succeeded'


def test_speech_preflight_fails_before_paid_start(settings):
    from karry_video.errors import VideoError
    class RejectingRenderer(FakeRenderer):
        def validate(self, project):
            raise VideoError('speech_too_long', '音声が長すぎます。', 422)
    provider, renderer = FakeProvider(), RejectingRenderer()
    app = create_app(settings, provider_factory=lambda *_: provider, renderer=renderer)
    renderer.storage = app.state.video_service.storage
    with TestClient(app) as client:
        project = make_project(client).json()
        response = client.post('/api/video/generate/render', json={'project_id': project['project_id']}, headers=HEADERS)
        job = wait_job(client, response.json()['job_id'])
        assert job['status'] == 'failed' and job['error_code'] == 'speech_too_long'
        assert provider.starts == 0


def test_definite_provider_rejection_releases_lock(settings):
    from karry_video.errors import VideoError
    class RejectedProvider(FakeProvider):
        async def start(self, project, image_path):
            self.starts += 1
            raise VideoError('provider_rejected', '入力が拒否されました。', 422)
    provider, renderer = RejectedProvider(), FakeRenderer()
    app = create_app(settings, provider_factory=lambda *_: provider, renderer=renderer)
    renderer.storage = app.state.video_service.storage
    with TestClient(app) as client:
        project = make_project(client).json()
        response = client.post('/api/video/generate/render', json={'project_id': project['project_id']}, headers=HEADERS)
        job = wait_job(client, response.json()['job_id'])
        assert job['status'] == 'failed'
        assert app.state.video_service.repository.get_project(project['project_id'])['active_job_id'] is None
        assert provider.starts == 1


def test_native_duration_rejected_before_job(client, monkeypatch):
    monkeypatch.setenv('REPLICATE_API_TOKEN', 'fake-only-not-used')
    project = make_project(client, duration_seconds='15').json()
    response = client.post('/api/video/generate/render', json={'project_id': project['project_id']}, headers=HEADERS)
    assert response.status_code == 422
    assert client.get(f"/api/video/projects/{project['project_id']}", headers=HEADERS).json()['status'] == 'draft'


def test_generated_scripts_table_is_created_on_video_database_start(client):
    from sqlalchemy import inspect

    engine = client.app.state.video_service.database.engine
    assert "generated_scripts" in inspect(engine).get_table_names()


def test_script_generation_saves_history_and_exposes_structured_scenes(client, monkeypatch):
    from karry_video import router

    calls = []
    def fake_generate(raw_input, scene_count):
        calls.append((raw_input, scene_count))
        return {
            "title": "雨の駅",
            "raw_input": raw_input,
            "narration_script": "少女は古い駅で青い傘を見つけた。\n遠くで汽笛が鳴った。",
            "visual_prompt": "雨の夜の小さな駅、青い傘、静かなシネマティックな画面。",
            "scenes": [
                {"scene_number": 1, "duration_seconds": 5, "narration": "少女は古い駅で青い傘を見つけた。", "visual_prompt": "雨のホームに青い傘が落ちている。"},
                {"scene_number": 2, "duration_seconds": 4, "narration": "遠くで汽笛が鳴った。", "visual_prompt": "少女が傘を拾い、霧の向こうを見る。"},
            ],
        }

    monkeypatch.setattr(router, "generate_script_breakdown", fake_generate)
    assert client.get("/api/video/scripts").status_code == 401
    response = client.post("/api/video/scripts/generate", json={"raw_input": "雨の夜、少女は駅に立っていた。", "scene_count": 2}, headers=HEADERS)
    assert response.status_code == 201
    script = response.json()
    assert calls == [("雨の夜、少女は駅に立っていた。", 2)]
    assert script["id"] > 0 and script["title"] == "雨の駅"
    assert len(script["scenes"]) == 2 and script["scenes"][1]["duration_seconds"] == 4
    assert "raw_input" in script and script["narration_script"].startswith("少女は")
    assert "created_at" in script
    listing = client.get("/api/video/scripts?limit=5", headers=HEADERS).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == script["id"]
    detail = client.get(f"/api/video/scripts/{script['id']}", headers=HEADERS)
    assert detail.status_code == 200 and detail.json()["scenes"] == script["scenes"]


@pytest.mark.parametrize("payload", [{"raw_input": "   "}, {"raw_input": "ok", "scene_count": 9}, {"raw_input": "x" * 12_001}])
def test_script_generation_rejects_invalid_inputs_before_ai_call(client, monkeypatch, payload):
    from karry_video import router

    def unexpected_call(*_):
        pytest.fail("AI should not run for invalid request data")

    monkeypatch.setattr(router, "generate_script_breakdown", unexpected_call)
    response = client.post("/api/video/scripts/generate", json=payload, headers=HEADERS)
    assert response.status_code == 422


def test_script_generator_requires_gemini_key(monkeypatch):
    from karry_video.errors import VideoError
    from karry_video.script_generation import generate_script_breakdown

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(VideoError) as error:
        generate_script_breakdown("雨の駅", 2)
    assert error.value.status_code == 503 and error.value.code == "script_ai_not_configured"
