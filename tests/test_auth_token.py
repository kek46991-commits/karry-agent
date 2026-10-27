import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent.auth_token import configured_token, normalize_token, token_matches
from karry_video.config import Settings
from karry_video.middleware import auth_error


@pytest.mark.parametrize("raw", ["synthetic-owner", "  synthetic-owner\n", "synthetic- owner", "\tsynthetic-owner\u3000", b" synthetic-owner "])
def test_whitespace_is_removed(raw):
    assert normalize_token(raw) == "synthetic-owner"


def test_blank_tokens_count_as_not_configured(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "  \n ")
    assert configured_token() == "" and not token_matches("", None) and not token_matches("x", "  ")
    assert TestClient(web.app).get("/api/sessions").status_code == 503


@pytest.mark.parametrize("env_value", ["synthetic-owner", " synthetic-owner \n", "synthetic- owner"])
@pytest.mark.parametrize("sent", ["synthetic-owner", "synthetic- owner", " synthetic-owner"])
def test_owner_routes_accept_tokens_with_stray_spaces(monkeypatch, env_value, sent):
    monkeypatch.setenv("KARRY_API_TOKEN", env_value)
    client = TestClient(web.app)
    assert client.get("/api/sessions", headers={"Authorization": f"Bearer {sent}"}).status_code == 200
    assert client.get("/api/knowledge/contexts", headers={"X-Owner-Token": sent}).status_code != 401


def test_wrong_tokens_are_still_rejected(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", " synthetic-owner ")
    client = TestClient(web.app)
    for value in ["synthetic-owne", "synthetic-owner2", "", "Synthetic-owner"]:
        assert client.get("/api/sessions", headers={"Authorization": f"Bearer {value}"}).status_code == 401, value
    assert client.post("/api/video/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/sessions", headers={"Authorization": "Basic synthetic-owner"}).status_code == 401


def test_upload_guard_and_video_projects_normalize(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner \n")
    client = TestClient(web.app)
    # Guarded upload path: correct token passes the guard and reaches validation (not 401/503).
    assert client.post("/api/video/jobs", headers={"Authorization": "Bearer synthetic-owner"}).status_code not in {401, 503}
    settings = Settings()
    assert settings.api_token == "synthetic-owner"
    assert auth_error(settings, "Bearer  synthetic-owner ") is None
    assert auth_error(settings, "Bearer other")[0] == 401
