import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import user_login

EMAIL = "owner@example.com"
PASSWORD = "synthetic-password"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    monkeypatch.delenv("KARRY_LOGIN_SECRET", raising=False)
    monkeypatch.setenv("KARRY_LOGIN_ACCOUNTS", f" Owner@Example.com | {user_login.hash_password(PASSWORD, iterations=1000)} ")
    user_login._FAILURES.clear()
    return TestClient(web.app)


def test_hash_round_trip_and_account_parsing(monkeypatch):
    stored = user_login.hash_password(PASSWORD, iterations=1000)
    assert PASSWORD not in stored and user_login.verify_password(stored, PASSWORD)
    assert not user_login.verify_password(stored, "wrong") and not user_login.verify_password("garbage", PASSWORD)
    monkeypatch.setenv("KARRY_LOGIN_ACCOUNTS", f"a@example.com|{stored}\nb@example.com|plain-text;broken")
    assert list(user_login.accounts()) == ["a@example.com"]


def test_login_returns_session_accepted_by_owner_routes(client):
    response = client.post("/api/login", json={"email": "OWNER@example.com ", "password": PASSWORD})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["email"] == EMAIL and body["token"].startswith(user_login.SESSION_PREFIX)
    auth = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/api/sessions", headers=auth).status_code == 200
    assert client.get("/api/admin/me", headers=auth).json() == {"admin": True}
    assert client.get("/api/login").json() == {"enabled": True}


def test_wrong_password_unknown_email_and_forged_tokens_are_rejected(client, monkeypatch):
    assert client.post("/api/login", json={"email": EMAIL, "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"email": "other@example.com", "password": PASSWORD}).status_code == 401
    token = client.post("/api/login", json={"email": EMAIL, "password": PASSWORD}).json()["token"]
    payload, _, _ = token[len(user_login.SESSION_PREFIX):].partition(".")
    for forged in (token[:-2] + "xx", f"{user_login.SESSION_PREFIX}{payload}.", "karrys1.", "synthetic-wrong"):
        assert client.get("/api/sessions", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    monkeypatch.setenv("KARRY_LOGIN_ACCOUNTS", "")
    assert client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_sessions_expire_and_die_with_owner_token_rotation(client, monkeypatch):
    token = user_login.issue_session(EMAIL, now=1000)
    assert user_login.session_email(token, now=1001) == EMAIL
    assert user_login.session_email(token, now=1000 + user_login.DEFAULT_SESSION_TTL + 1) is None
    fresh = user_login.issue_session(EMAIL)
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-rotated")
    assert user_login.session_email(fresh) is None


def test_login_disabled_without_accounts_or_owner_token(client, monkeypatch):
    monkeypatch.setenv("KARRY_LOGIN_ACCOUNTS", "")
    assert client.get("/api/login").json() == {"enabled": False}
    assert client.post("/api/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 503
    monkeypatch.setenv("KARRY_API_TOKEN", "")
    assert client.post("/api/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 503


def test_repeated_failures_are_throttled(client):
    for _ in range(user_login.MAX_FAILURES):
        assert client.post("/api/login", json={"email": EMAIL, "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 429


def test_session_token_passes_upload_guard(client):
    token = client.post("/api/login", json={"email": EMAIL, "password": PASSWORD}).json()["token"]
    response = client.post("/api/upload_image", headers={"Authorization": f"Bearer {token}"}, data={"session_id": "missing"})
    assert response.status_code not in (401, 503)
