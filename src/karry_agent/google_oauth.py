"""Web (one-click) Google OAuth connect: the account owner approves on Google's own
consent screen, and the resulting refresh token is stored encrypted so the agent can
read their data (Photos) afterwards without re-login.

No password or 2FA is ever bypassed — the human completes Google's normal sign-in and
consent in their browser; we only persist the refresh token Google hands back.

Token storage: encrypted (Fernet) in the private R2/S3 bucket already used for tasks,
or ``GOOGLE_OAUTH_TOKEN_JSON`` env as a fallback. The encryption key is
``KARRY_GOOGLE_TOKEN_KEY`` (urlsafe base64, 32 bytes) or derived from the owner token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import urlencode

import requests

from .website_storage import StorageError, load_storage_config

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
OPENID_SCOPES = ["openid", "email"]
DEFAULT_SCOPES = [PICKER_SCOPE]
TOKEN_OBJECT = "secure/google-oauth-token.enc"
STATE_TTL = 600


def web_client() -> tuple[str, str]:
    """(client_id, client_secret) for the Web-application OAuth client, or ('','')."""
    client_id = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    secret = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    if not client_id or not secret:
        raw = os.getenv("GOOGLE_OAUTH_CLIENT_JSON") or ""
        if raw.strip():
            try:
                data = json.loads(raw)
                data = data.get("web", data)
                client_id = str(data.get("client_id", "")).strip()
                secret = str(data.get("client_secret", "")).strip()
            except (ValueError, AttributeError):
                return "", ""
    return client_id, secret


def configured() -> bool:
    return all(web_client())


def scopes() -> list[str]:
    extra = CALENDAR_SCOPE if os.getenv("GOOGLE_OAUTH_WITH_CALENDAR") == "1" else None
    picked = [s for s in (os.getenv("GOOGLE_OAUTH_SCOPES") or "").replace(",", " ").split() if s] or list(DEFAULT_SCOPES)
    ordered = OPENID_SCOPES + picked + ([extra] if extra else [])
    seen: dict[str, None] = {}
    for scope in ordered:
        seen.setdefault(scope, None)
    return list(seen)


def _signing_key() -> bytes:
    key = "".join((os.getenv("KARRY_LOGIN_SECRET") or os.getenv("KARRY_API_TOKEN") or "").split())
    return key.encode()


def sign_state(*, now: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"n": base64.urlsafe_b64encode(os.urandom(9)).decode(),
        "exp": int((now or time.time()) + STATE_TTL)}, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{payload}.{signature}"


def verify_state(state: str | None, *, now: float | None = None) -> bool:
    payload, _, signature = str(state or "").partition(".")
    key = _signing_key()
    if not key or not signature:
        return False
    expected = base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) > (now or time.time())
    except Exception:
        return False


def authorize_url(redirect_uri: str, state: str) -> str:
    client_id, _ = web_client()
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(scopes()), "access_type": "offline", "include_granted_scopes": "true",
              "prompt": "consent", "state": state}
    return f"{AUTH_URI}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict[str, Any]:
    client_id, secret = web_client()
    response = requests.post(TOKEN_URI, data={"code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=(5, 30))
    response.raise_for_status()
    tokens = response.json()
    if not tokens.get("refresh_token"):
        raise ValueError("Googleがrefresh_tokenを返しませんでした。アクセスを一度解除してから再度お試しください。")
    email = ""
    try:
        info = requests.get(USERINFO_URI, headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=(5, 30))
        if info.ok:
            email = str(info.json().get("email", ""))
    except requests.RequestException:
        pass
    return {"client_id": client_id, "client_secret": secret, "refresh_token": tokens["refresh_token"],
            "scopes": scopes(), "email": email, "connected_at": int(time.time())}


def _fernet():
    from cryptography.fernet import Fernet

    raw = (os.getenv("KARRY_GOOGLE_TOKEN_KEY") or "").strip()
    if raw:
        key = raw.encode()
    else:
        seed = _signing_key()
        if not seed:
            raise ValueError("KARRY_GOOGLE_TOKEN_KEY または KARRY_API_TOKEN が必要です。")
        key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


def _bucket():
    config = load_storage_config()
    from .website_storage import _client

    return _client(config), config.bucket, config.prefix + TOKEN_OBJECT


def save_token(info: dict[str, Any]) -> None:
    blob = _fernet().encrypt(json.dumps(info, separators=(",", ":")).encode())
    try:
        client, bucket, key = _bucket()
        client.put_object(Bucket=bucket, Key=key, Body=blob, ContentType="application/octet-stream")
    except StorageError:
        raise
    except Exception as exc:  # pragma: no cover - network/credingtial issues surface as storage errors
        raise StorageError("Google連携トークンを保存できませんでした。", "storage_unknown") from exc


def load_token() -> dict[str, Any] | None:
    raw = os.getenv("GOOGLE_OAUTH_TOKEN_JSON")
    if raw and raw.strip():
        try:
            return json.loads(raw)
        except ValueError:
            return None
    try:
        client, bucket, key = _bucket()
        blob = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except StorageError:
        return None
    except Exception:
        return None
    try:
        return json.loads(_fernet().decrypt(blob))
    except Exception:
        return None


def disconnect() -> bool:
    try:
        client, bucket, key = _bucket()
        client.delete_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def status() -> dict[str, Any]:
    token = load_token()
    return {"configured": configured(), "connected": bool(token and token.get("refresh_token")),
            "email": (token or {}).get("email", ""), "scopes": (token or {}).get("scopes", []),
            "connected_at": (token or {}).get("connected_at")}
