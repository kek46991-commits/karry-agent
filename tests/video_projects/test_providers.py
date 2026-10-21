import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from karry_video.config import Settings
from karry_video.errors import SubmissionUnknown, VideoError
from karry_video.providers import ReplicateKlingProvider, RunwayProvider, select_provider


def run(coro):
    return asyncio.run(coro)


def project(**overrides):
    result = {
        "render_prompt": "slow cinematic camera move",
        "duration_seconds": 5,
        "aspect_ratio": "9:16",
        "tts": {"text": "this is intentionally ignored by video validation"},
    }
    result.update(overrides)
    return result


def png(tmp_path: Path, name: str = "start.png", extra: bytes = b"") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + extra)
    return path


def mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2avc1mp41"


def test_constructor_performs_no_network_and_hides_secret():
    calls = []

    def handler(request):
        calls.append(request)
        raise AssertionError("constructor must not send HTTP")

    secret = "replicate-very-secret-token"
    adapter = ReplicateKlingProvider(Settings(), secret, transport=httpx.MockTransport(handler))
    assert calls == []
    assert secret not in repr(adapter)
    assert adapter.name == "replicate"


def test_select_provider_auto_priority_and_direct_kling_safety(monkeypatch):
    monkeypatch.setenv("REPLICATE_API_TOKEN", "replicate-secret")
    monkeypatch.setenv("RUNWAY_API_KEY", "runway-secret")
    assert select_provider("auto", Settings()).name == "replicate"
    assert select_provider("kling", Settings()).name == "replicate"

    monkeypatch.delenv("REPLICATE_API_TOKEN")
    assert select_provider("auto", Settings()).name == "runway"

    monkeypatch.delenv("RUNWAY_API_KEY")
    monkeypatch.setenv("KLING_API_KEY", "native-kling-secret")
    with pytest.raises(VideoError) as error:
        select_provider("auto", Settings())
    assert error.value.code == "provider_not_configured"
    assert error.value.status_code == 503
    assert "native-kling-secret" not in error.value.message


@pytest.mark.parametrize("name", ["auto", "replicate", "runway", "kling", "unknown"])
def test_select_provider_unconfigured_is_safe_503(monkeypatch, name):
    for key in ("REPLICATE_API_TOKEN", "RUNWAY_API_KEY", "KLING_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(VideoError) as error:
        select_provider(name, Settings())
    assert error.value.code == "provider_not_configured"
    assert error.value.status_code == 503


def test_replicate_start_payload_rounds_native_duration_and_calls_once(tmp_path):
    image = png(tmp_path)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.replicate.com/v1/models/kwaivgi/kling-v2.1/predictions"
        assert request.headers["authorization"] == "Bearer replicate-secret"
        body = json.loads(request.content)
        assert body["input"]["prompt"] == "slow cinematic camera move"
        assert body["input"]["mode"] == "standard"
        assert body["input"]["duration"] == 10
        assert body["input"]["start_image"].startswith("data:image/png;base64,")
        return httpx.Response(201, json={"id": "prediction_123"})

    adapter = ReplicateKlingProvider(Settings(), "replicate-secret", transport=httpx.MockTransport(handler))
    assert run(adapter.start(project(duration_seconds=5.01), image)) == "prediction_123"
    assert len(seen) == 1


def test_runway_start_payload_uses_fixed_version_ratio_and_native_duration(tmp_path):
    image = png(tmp_path)

    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.dev.runwayml.com/v1/image_to_video"
        assert request.headers["authorization"] == "Bearer runway-secret"
        assert request.headers["x-runway-version"] == "2024-11-06"
        body = json.loads(request.content)
        assert body == {
            "model": "gen4_turbo",
            "promptImage": body["promptImage"],
            "promptText": "slow cinematic camera move",
            "ratio": "1280:720",
            "duration": 2,
        }
        assert body["promptImage"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"id": "task-123"})

    adapter = RunwayProvider(Settings(), "runway-secret", transport=httpx.MockTransport(handler))
    assert run(adapter.start(project(duration_seconds=1.01, aspect_ratio="16:9"), image)) == "task-123"


@pytest.mark.parametrize(
    ("adapter_cls", "token", "response"),
    [
        (ReplicateKlingProvider, "replicate-secret", httpx.Response(200, content=b"not json")),
        (RunwayProvider, "runway-secret", httpx.Response(500, text="provider returned secret text")),
    ],
)
def test_start_ambiguous_results_raise_submission_unknown_once(tmp_path, adapter_cls, token, response):
    image = png(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return response

    adapter = adapter_cls(Settings(), token, transport=httpx.MockTransport(handler))
    with pytest.raises(SubmissionUnknown) as error:
        run(adapter.start(project(), image))
    assert calls == 1
    assert token not in str(error.value)
    assert "secret text" not in error.value.message


def test_start_with_no_accepted_id_stops_without_retry(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"status": "starting"})

    adapter = ReplicateKlingProvider(Settings(), "replicate-secret", transport=httpx.MockTransport(handler))
    with pytest.raises(SubmissionUnknown):
        run(adapter.start(project(), png(tmp_path)))
    assert calls == 1


@pytest.mark.parametrize("adapter_cls, token", [(ReplicateKlingProvider, "r-secret"), (RunwayProvider, "w-secret")])
def test_start_confirmed_rejection_is_safe_and_redacted(tmp_path, adapter_cls, token):
    def handler(request):
        return httpx.Response(422, text=f"raw provider body contains {token}")

    adapter = adapter_cls(Settings(), token, transport=httpx.MockTransport(handler))
    with pytest.raises(VideoError) as error:
        run(adapter.start(project(), png(tmp_path)))
    assert error.value.code == "provider_rejected"
    assert error.value.status_code == 422
    assert token not in error.value.message
    assert token not in str(error.value)
    assert token not in repr(adapter)


def test_validate_is_side_effect_free_rejects_over_ten_and_ignores_tts():
    adapter = RunwayProvider(Settings(), "runway-secret", transport=httpx.MockTransport(lambda _: (_ for _ in ()).throw(AssertionError())))
    adapter.validate(project(duration_seconds=2, tts={"bad": object()}))
    with pytest.raises(VideoError) as error:
        adapter.validate(project(duration_seconds=10.01))
    assert error.value.code == "invalid_duration"
    assert error.value.status_code == 422


def test_start_rejects_oversized_or_non_png_image_before_submission(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(201, json={"id": "would-not-be-used"})

    too_large = png(tmp_path, extra=b"x" * (1024 * 1024))
    adapter = ReplicateKlingProvider(Settings(), "token", transport=httpx.MockTransport(handler))
    with pytest.raises(VideoError) as error:
        run(adapter.start(project(), too_large))
    assert error.value.code == "image_too_large"
    assert calls == 0

    jpg = tmp_path / "start.jpg"
    jpg.write_bytes(b"not a png")
    with pytest.raises(VideoError) as error:
        run(adapter.start(project(), jpg))
    assert error.value.code == "invalid_image"
    assert calls == 0


@pytest.mark.parametrize(
    ("adapter_cls", "token", "job_id", "response", "expected"),
    [
        (ReplicateKlingProvider, "r", "prediction_1", {"id": "prediction_1", "status": "starting"}, "queued"),
        (ReplicateKlingProvider, "r", "prediction_1", {"id": "prediction_1", "status": "processing"}, "running"),
        (ReplicateKlingProvider, "r", "prediction_1", {"id": "prediction_1", "status": "failed"}, "failed"),
        (RunwayProvider, "w", "task-1", {"id": "task-1", "status": "PENDING"}, "queued"),
        (RunwayProvider, "w", "task-1", {"id": "task-1", "status": "RUNNING"}, "running"),
        (RunwayProvider, "w", "task-1", {"id": "task-1", "status": "CANCELED"}, "canceled"),
    ],
)
def test_poll_maps_documented_provider_states(adapter_cls, token, job_id, response, expected):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=response)

    adapter = adapter_cls(Settings(), token, transport=httpx.MockTransport(handler))
    state = run(adapter.poll(job_id))
    assert state.status == expected
    assert state.output_url is None


def test_poll_success_only_returns_allowlisted_ephemeral_url():
    url = "https://abc.replicate.delivery/output.mp4?signature=private"

    def handler(request):
        return httpx.Response(200, json={"id": "prediction_1", "status": "succeeded", "output": [url]})

    adapter = ReplicateKlingProvider(Settings(), "r", transport=httpx.MockTransport(handler))
    state = run(adapter.poll("prediction_1"))
    assert state.status == "succeeded"
    assert state.output_url == url


def test_poll_rejects_mismatched_id_and_unapproved_output_without_body_leak():
    def mismatch(request):
        return httpx.Response(200, json={"id": "another-job", "status": "succeeded"})

    adapter = RunwayProvider(Settings(), "runway-secret", transport=httpx.MockTransport(mismatch))
    with pytest.raises(VideoError) as error:
        run(adapter.poll("task-1"))
    assert error.value.code == "provider_protocol_error"

    def ssrf(request):
        return httpx.Response(200, json={"id": "prediction_1", "status": "succeeded", "output": "http://127.0.0.1/private"})

    adapter = ReplicateKlingProvider(Settings(), "replicate-secret", transport=httpx.MockTransport(ssrf))
    with pytest.raises(VideoError) as error:
        run(adapter.poll("prediction_1"))
    assert error.value.code == "invalid_provider_output"
    assert "127.0.0.1" not in error.value.message


def test_download_is_unauthenticated_streamed_and_atomic(tmp_path):
    destination = tmp_path / "result.mp4"
    url = "https://abc.replicate.delivery/output.mp4?signature=not-to-log"

    def handler(request):
        assert request.method == "GET"
        assert request.headers.get("authorization") is None
        assert request.headers.get("cookie") is None
        assert request.headers["accept"] == "video/mp4"
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=mp4_bytes())

    adapter = ReplicateKlingProvider(Settings(), "replicate-secret", transport=httpx.MockTransport(handler))
    run(adapter.download(url, destination))
    assert destination.read_bytes() == mp4_bytes()


@pytest.mark.parametrize(
    ("url", "response"),
    [
        ("https://evil.example/file.mp4", None),
        ("https://abc.replicate.delivery/redirect", httpx.Response(302, headers={"location": "https://evil.example/"})),
        ("https://abc.replicate.delivery/html", httpx.Response(200, content=b"<html>not video</html>")),
        ("https://abc.replicate.delivery/large", httpx.Response(200, headers={"content-length": str(256 * 1024 * 1024 + 1)}, content=b"")),
    ],
)
def test_download_blocks_ssrf_redirect_bad_file_and_size(tmp_path, url, response):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert response is not None
        return response

    destination = tmp_path / "result.mp4"
    adapter = ReplicateKlingProvider(Settings(), "secret", transport=httpx.MockTransport(handler))
    with pytest.raises(VideoError) as error:
        run(adapter.download(url, destination))
    assert error.value.code == "invalid_provider_output"
    assert not destination.exists()
    if "evil.example" in url:
        assert calls == 0
    else:
        assert calls == 1
