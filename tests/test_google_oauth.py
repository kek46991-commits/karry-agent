import json
import time

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import google_oauth

OWNER = "synthetic-owner"


class FakeBucket:
    """In-memory stand-in for the S3/R2 token object."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body, **_):
        self.store[Key] = Body

    def get_object(self, *, Bucket, Key):
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": _Body(self.store[Key])}

    def delete_object(self, *, Bucket, Key):
        self.store.pop(Key, None)


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


@pytest.fixture
def storage(monkeypatch):
    bucket = FakeBucket()
    monkeypatch.setenv("KARRY_API_TOKEN", OWNER)
    monkeypatch.delenv("KARRY_LOGIN_SECRET", raising=False)
    monkeypatch.delenv("KARRY_GOOGLE_TOKEN_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_TOKEN_JSON", raising=False)
    monkeypatch.setattr(google_oauth, "_bucket", lambda: (bucket, "karry", "p/" + google_oauth.TOKEN_OBJECT))
    return bucket


@pytest.fixture
def client(storage, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://karry-agent-1.onrender.com/api/google/oauth/callback")
    return TestClient(web.app)


def test_state_round_trip(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", OWNER)
    state = google_oauth.sign_state()
    assert google_oauth.verify_state(state)
    assert not google_oauth.verify_state(state + "x")
    assert not google_oauth.verify_state(None)
    assert not google_oauth.verify_state("no.dot.sig")


def test_state_expires(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", OWNER)
    state = google_oauth.sign_state(now=time.time() - google_oauth.STATE_TTL - 10)
    assert not google_oauth.verify_state(state)


def test_state_rejected_when_key_rotated(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", OWNER)
    state = google_oauth.sign_state()
    monkeypatch.setenv("KARRY_API_TOKEN", "different")
    assert not google_oauth.verify_state(state)


def test_authorize_url_shape(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    url = google_oauth.authorize_url("https://x/cb", "state123")
    assert url.startswith(google_oauth.AUTH_URI + "?")
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "redirect_uri=https%3A%2F%2Fx%2Fcb" in url and "state=state123" in url
    assert google_oauth.PICKER_SCOPE.replace(":", "%3A").replace("/", "%2F") in url


def test_token_persist_round_trip_encrypted(storage):
    info = {"client_id": "cid", "client_secret": "sec", "refresh_token": "r", "email": "a@b.com",
            "scopes": [google_oauth.PICKER_SCOPE], "connected_at": 1}
    google_oauth.save_token(info)
    blob = next(iter(storage.store.values()))
    assert b"refresh_token" not in blob  # stored encrypted, not plaintext
    assert google_oauth.load_token() == info
    assert google_oauth.disconnect() is True
    assert google_oauth.load_token() is None


def test_exchange_code_uses_refresh_token(storage, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    calls = {}

    class Resp:
        ok = True

        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    def fake_post(url, data=None, timeout=None):
        calls["post"] = (url, data)
        return Resp({"access_token": "at", "refresh_token": "rt"})

    def fake_get(url, headers=None, timeout=None):
        return Resp({"email": "owner@example.com"})

    monkeypatch.setattr(google_oauth.requests, "post", fake_post)
    monkeypatch.setattr(google_oauth.requests, "get", fake_get)
    info = google_oauth.exchange_code("the-code", "https://x/cb")
    assert info["refresh_token"] == "rt" and info["email"] == "owner@example.com"
    assert calls["post"][1]["grant_type"] == "authorization_code"


def test_exchange_code_requires_refresh_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")

    class Resp:
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "at"}

    monkeypatch.setattr(google_oauth.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(ValueError):
        google_oauth.exchange_code("code", "https://x/cb")


def test_start_requires_auth(client):
    assert client.get("/api/google/oauth/start").status_code == 401
    assert client.get("/api/google/status").status_code == 401
    assert client.post("/api/google/disconnect").status_code == 401


def test_start_returns_authorize_url(client):
    resp = client.get("/api/google/oauth/start", headers={"Authorization": f"Bearer {OWNER}"})
    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith(google_oauth.AUTH_URI)


def test_start_503_when_unconfigured(storage, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    c = TestClient(web.app)
    assert c.get("/api/google/oauth/start", headers={"Authorization": f"Bearer {OWNER}"}).status_code == 503


def test_callback_rejects_bad_state(client):
    resp = client.get("/api/google/oauth/callback", params={"code": "x", "state": "bad"})
    assert resp.status_code == 400 and "有効期限" in resp.text


def test_callback_handles_denial(client):
    resp = client.get("/api/google/oauth/callback", params={"error": "access_denied"})
    assert resp.status_code == 400 and "キャンセル" in resp.text


def test_callback_success_persists_token(client, storage, monkeypatch):
    monkeypatch.setattr(google_oauth, "exchange_code",
                        lambda code, redirect: {"client_id": "cid", "client_secret": "sec",
                                                "refresh_token": "rt", "email": "owner@example.com",
                                                "scopes": [], "connected_at": 1})
    state = google_oauth.sign_state()
    resp = client.get("/api/google/oauth/callback", params={"code": "the-code", "state": state})
    assert resp.status_code == 200 and "連携" in resp.text
    assert google_oauth.load_token()["refresh_token"] == "rt"
    status = client.get("/api/google/status", headers={"Authorization": f"Bearer {OWNER}"}).json()
    assert status["connected"] is True and status["email"] == "owner@example.com"
