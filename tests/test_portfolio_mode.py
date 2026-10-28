import pytest
from fastapi.testclient import TestClient

import app as app_module

EXPERIMENTAL = ["/api/video/config", "/api/video/jobs", "/api/video/autopilot", "/api/media", "/api/optimus/status", "/api/agent/self-correction", "/api/webtoon/jobs"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    return TestClient(app_module.app)


AUTH = {"Authorization": "Bearer synthetic-owner"}


def test_experimental_features_are_off_by_default(client, monkeypatch):
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES", raising=False)
    monkeypatch.delenv("KARRY_WEBTOON", raising=False)
    assert client.get("/api/features").json() == {"experimental": False, "webtoon": False}
    for path in EXPERIMENTAL:
        assert client.get(path, headers=AUTH).status_code == 404, path
        assert client.post(path, headers=AUTH).status_code == 404, path
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith(("/api/video", "/api/media", "/api/optimus", "/api/agent/self-correction", "/api/webtoon"))]
    assert "/api/sessions" in paths and "/api/chat" in paths
    assert client.get("/health").status_code == 200


def test_core_ui_has_no_video_form_and_hides_chat_video_hint(client, monkeypatch):
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES", raising=False)
    html = client.get("/").text
    assert 'id="videoSuite"' not in html and 'id="autopilot-form"' not in html
    assert '<p id="videoChatHint" class="hint chat-video-hint" hidden>' in html
    assert "loadFeatures()" in html


def test_sandbox_flag_restores_experimental_routes(client, monkeypatch):
    monkeypatch.setenv("KARRY_EXPERIMENTAL_FEATURES", "1")
    assert client.get("/api/features").json() == {"experimental": True, "webtoon": True}
    assert client.get("/api/video/config", headers=AUTH).status_code == 200
    assert client.get("/api/video/config").status_code == 401
    assert any(p.startswith("/api/video") for p in client.get("/openapi.json").json()["paths"])


def test_gate_does_not_match_lookalike_paths():
    from karry_agent.feature_flags import is_experimental_path

    assert is_experimental_path("/api/video") and is_experimental_path("/api/optimus/clones/1/run")
    assert not is_experimental_path("/api/videos-public") and not is_experimental_path("/api/mediation")


def test_webtoon_flag_publishes_only_the_webtoon_api(client, monkeypatch):
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES", raising=False)
    monkeypatch.setenv("KARRY_WEBTOON", "1")
    assert client.get("/api/features").json() == {"experimental": False, "webtoon": True}
    missing = client.get("/api/webtoon/jobs/" + "0" * 32, headers=AUTH)
    assert missing.status_code == 404 and missing.json()["detail"] != "Not Found"  # route 404, not the feature gate
    assert client.get("/api/webtoon/jobs/" + "0" * 32).status_code == 401
    for path in [p for p in EXPERIMENTAL if not p.startswith("/api/webtoon")]:
        assert client.get(path, headers=AUTH).status_code == 404, path
    paths = client.get("/openapi.json").json()["paths"]
    assert any(p.startswith("/api/webtoon") for p in paths)
    assert not [p for p in paths if p.startswith(("/api/video", "/api/media", "/api/optimus", "/api/agent/self-correction"))]
