import io
import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import app as web
from karry_agent import video_suite as vs
from karry_agent.video_suite_api import build_router

TOKEN = "synthetic-sadtalker-owner"
AUTH = {"Authorization": "Bearer " + TOKEN}
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"mock-video"


def portrait():
    data = io.BytesIO()
    Image.new("RGB", (80, 120), (210, 80, 20)).save(data, "PNG")
    return data.getvalue()


class Speaker:
    available = True
    name = "test-tts"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.wav = vs.placeholder_wav()

    def synthesize(self, text, voice):
        self.calls += 1
        if self.fail:
            raise vs.VideoError("synthetic failure", 502, "tts_failed")
        return self.wav


class Renderer:
    name = "local"
    local = True
    clip_lengths = (120,)

    def __init__(self):
        self.calls = []

    def render(self, prompt, aspect, seconds, image, audio):
        self.calls.append((image, audio, aspect, seconds))
        return MP4


class LipSync:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def generate(self, image, audio):
        self.calls.append((image, audio))
        if self.error:
            raise self.error
        return MP4


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.delenv("KARRY_SADTALKER_SPACE", raising=False)
    vs._MEMORY_DOCS.clear()
    vs._MEMORY_BLOBS.clear()

    def make(lip=None, speaker=None, normalizer=None, paid=False):
        renderer = Renderer()
        speaker = speaker or Speaker()
        lip = lip or LipSync()
        monkeypatch.setattr(vs, "LocalVideoRenderer", lambda: renderer)
        paid_calls = []

        def adapters():
            if paid:
                paid_calls.append(True)
                raise AssertionError("paid provider must not be selected")
            return renderer

        app = FastAPI()
        app.include_router(build_router(web.require_tool_auth, lambda sid: None, adapters, lambda: speaker,
                                        lambda fn: fn(), lambda: lip, normalizer or (lambda *args: MP4)))
        return TestClient(app), renderer, speaker, lip, paid_calls

    return make


def create(client, *, lip=True, with_image=True, narration="Hello", rid=None, headers=AUTH):
    return client.post("/api/video/jobs", headers=headers, data={"session_id": "lipsync-test-task", "prompt": "Portrait",
        "aspect_ratio": "9:16", "duration_seconds": "5", "voice": "jimmy", "narration_text": narration,
        "lip_sync": str(lip).lower(), "sadtalker": str(lip).lower(), "request_id": str(rid or uuid4())},
        files={"image": ("portrait.png", portrait(), "image/png")} if with_image else None)


@pytest.mark.parametrize("with_image,narration", [(False, "Hello"), (True, ""), (False, "")])
def test_missing_lipsync_inputs_rejected_before_processing(setup, with_image, narration):
    client, renderer, speaker, lip, _ = setup()
    response = create(client, with_image=with_image, narration=narration)
    assert response.status_code == 422
    assert response.json()["code"] == "lipsync_input_required"
    assert not renderer.calls and speaker.calls == 0 and not lip.calls


def test_lipsync_success_keeps_same_voice_and_avoids_paid_video(setup):
    client, renderer, speaker, lip, paid = setup(paid=True)
    response = create(client)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "succeeded"
    assert job["provider"] == "local"
    assert job["lip_sync"] == {"requested": True, "status": "succeeded", "engine": "sadtalker"}
    assert speaker.calls == 1 and len(lip.calls) == 1
    assert lip.calls[0][1] == speaker.wav and lip.calls[0][0].startswith(b"\xff\xd8")
    assert not renderer.calls and not paid
    clip = client.get(job["segments"][0]["url"], headers=AUTH)
    assert clip.status_code == 200 and clip.content == MP4


@pytest.mark.parametrize("code", ["sadtalker_timeout", "sadtalker_busy", "sadtalker_unavailable", "sadtalker_configuration"])
def test_external_failure_uses_same_audio_without_resubmission(setup, code):
    from karry_agent.sadtalker import SadTalkerError
    client, renderer, speaker, lip, _ = setup(lip=LipSync(SadTalkerError(code)))
    response = create(client)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "succeeded"
    assert job["lip_sync"]["status"] == "fallback"
    assert job["lip_sync"]["engine"] == "local"
    assert job["lip_sync"]["fallback_reason"] == code
    assert len(lip.calls) == len(renderer.calls) == speaker.calls == 1
    assert renderer.calls[0][1] == speaker.wav


def test_unknown_external_exception_is_sanitized(setup):
    client, renderer, speaker, lip, _ = setup(lip=LipSync(RuntimeError("secret-signed-url-and-token")))
    response = create(client)
    assert response.json()["lip_sync"]["fallback_reason"] == "sadtalker_failed"
    assert "secret-signed-url-and-token" not in response.text
    assert len(renderer.calls) == speaker.calls == 1


def test_bad_external_mp4_uses_local_fallback(setup):
    def broken(*args):
        raise vs.VideoError("bad media", 502, "sadtalker_output")
    client, renderer, _, _, _ = setup(normalizer=broken)
    response = create(client)
    assert response.json()["lip_sync"]["status"] == "fallback"
    assert response.json()["lip_sync"]["fallback_reason"] == "sadtalker_output"
    assert len(renderer.calls) == 1


def test_failed_tts_never_uploads_placeholder_to_sadtalker(setup):
    client, renderer, speaker, lip, _ = setup(speaker=Speaker(fail=True))
    response = create(client)
    job = response.json()
    assert job["status"] == "partial"
    assert job["lip_sync"]["fallback_reason"] == "audio_unavailable"
    assert job["narration"]["status"] == "placeholder"
    assert not lip.calls and speaker.calls == 1 and len(renderer.calls) == 1


def test_off_and_idempotent_retry_do_not_send_external_request_twice(setup):
    client, renderer, speaker, lip, _ = setup()
    off = create(client, lip=False)
    assert off.status_code == 202 and not lip.calls
    rid = uuid4()
    first = create(client, rid=rid)
    repeated = create(client, rid=rid)
    assert repeated.status_code == 200 and first.json()["id"] == repeated.json()["id"]
    assert len(lip.calls) == 1 and speaker.calls == 2


def test_owner_auth_and_config_do_not_expose_provider_secret(setup, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "synthetic-hf-secret")
    client, _, _, _, _ = setup()
    assert create(client, headers={}).status_code == 401
    config = client.get("/api/video/config", headers=AUTH)
    assert config.status_code == 200 and "synthetic-hf-secret" not in config.text
    assert config.json()["sadtalker"]["configured"] is False


def test_real_mp4_normalizer_preserves_dimensions_and_audio(tmp_path):
    import subprocess
    renderer = vs.LocalVideoRenderer()
    clip = renderer.render("Portrait", "9:16", 1, portrait(), None)
    output = vs.normalize_lipsync_video(clip, vs.placeholder_wav(), "16:9", 2)
    target = tmp_path / "lipsync-normalized.mp4"
    target.write_bytes(output)
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(target)]))
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1280, 720)
    assert any(stream["codec_type"] == "audio" for stream in probe["streams"])
    assert abs(float(probe["format"]["duration"]) - 2) < 0.1


def test_unconfigured_space_produces_real_local_fallback_mp4(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.delenv("KARRY_SADTALKER_SPACE", raising=False)
    vs._MEMORY_DOCS.clear()
    vs._MEMORY_BLOBS.clear()
    speaker = Speaker()
    app = FastAPI()
    app.include_router(build_router(web.require_tool_auth, lambda sid: None,
                                    narrator_factory=lambda: speaker, runner=lambda fn: fn()))
    client = TestClient(app)
    response = create(client)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "succeeded" and job["lip_sync"]["status"] == "fallback"
    assert speaker.calls == 1
    media = client.get(job["segments"][0]["url"], headers=AUTH)
    assert media.status_code == 200
    target = tmp_path / "unconfigured-local-fallback.mp4"
    target.write_bytes(media.content)
    probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(target)]))
    assert abs(float(probe["format"]["duration"]) - 5) < 0.1
    assert any(stream["codec_type"] == "audio" for stream in probe["streams"])
