"""Email + password login for the owner UI.

Accounts live in the environment (never in git): ``KARRY_LOGIN_ACCOUNTS`` holds
``email|pbkdf2_sha256$<iterations>$<salt>$<hash>`` entries separated by newlines,
commas or semicolons. A successful login returns a signed, expiring session
token that the owner APIs accept in place of ``KARRY_API_TOKEN``, so a phone can
log in with a password instead of the long owner token.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from functools import lru_cache

ALGORITHM = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 600_000
SESSION_PREFIX = "karrys1."
DEFAULT_SESSION_TTL = 30 * 24 * 3600
MAX_SESSION_TTL = 90 * 24 * 3600
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60
_FAILURES: dict[str, list[float]] = {}
_FAILURE_LOCK = threading.Lock()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_email(value: str | None) -> str:
    return "".join(str(value or "").split()).lower()


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    """Return the storable ``pbkdf2_sha256$...`` string for a password."""
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(stored: str, password: str) -> bool:
    try:
        algorithm, iterations, salt, digest = str(stored or "").strip().split("$")
        if algorithm != ALGORITHM:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", str(password or "").encode(), _unb64(salt), int(iterations))
    except Exception:
        return False
    return hmac.compare_digest(candidate, _unb64(digest))


def accounts() -> dict[str, str]:
    """Configured accounts as ``{email: password_hash}`` (invalid entries are ignored)."""
    found: dict[str, str] = {}
    raw = os.getenv("KARRY_LOGIN_ACCOUNTS", "")
    for entry in raw.replace("\n", ",").replace(";", ",").split(","):
        email, separator, stored = entry.strip().partition("|")
        email, stored = normalize_email(email), "".join(stored.split())
        if separator and email and stored.startswith(f"{ALGORITHM}$"):
            found[email] = stored
    return found


def login_enabled() -> bool:
    return bool(accounts())


def _signing_key() -> bytes:
    """Sessions are signed with a dedicated secret, or with the owner token when none is set."""
    key = "".join(os.getenv("KARRY_LOGIN_SECRET", "").split()) or "".join(os.getenv("KARRY_API_TOKEN", "").split())
    return key.encode()


def _session_ttl() -> int:
    try:
        ttl = int(os.getenv("KARRY_LOGIN_SESSION_TTL", DEFAULT_SESSION_TTL))
    except ValueError:
        return DEFAULT_SESSION_TTL
    return max(60, min(ttl, MAX_SESSION_TTL))


def issue_session(email: str, *, now: float | None = None) -> str:
    key = _signing_key()
    if not key:
        raise RuntimeError("KARRY_API_TOKEN or KARRY_LOGIN_SECRET is required to sign login sessions")
    payload = _b64(json.dumps({"email": normalize_email(email), "exp": int((now or time.time()) + _session_ttl())},
                              separators=(",", ":"), sort_keys=True).encode())
    return f"{SESSION_PREFIX}{payload}.{_b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())}"


def session_email(token: str | None, *, now: float | None = None) -> str | None:
    """Email of a valid, unexpired session whose account still exists, else ``None``."""
    token = "".join(str(token or "").split())
    key = _signing_key()
    if not key or not token.startswith(SESSION_PREFIX):
        return None
    payload, _, signature = token[len(SESSION_PREFIX):].partition(".")
    expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    if not signature or not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_unb64(payload))
        email, expires = normalize_email(claims["email"]), float(claims["exp"])
    except Exception:
        return None
    if expires <= (now or time.time()) or email not in accounts():
        return None
    return email


@lru_cache(maxsize=1)
def _unknown_account_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def throttled(*keys: str, now: float | None = None) -> bool:
    """True while any key (client IP, email) has ``MAX_FAILURES`` recent failed logins."""
    now = now or time.time()
    with _FAILURE_LOCK:
        for key in keys:
            recent = [t for t in _FAILURES.get(key, []) if now - t < FAILURE_WINDOW]
            _FAILURES[key] = recent
            if len(recent) >= MAX_FAILURES:
                return True
    return False


def record_failure(*keys: str, now: float | None = None) -> None:
    with _FAILURE_LOCK:
        for key in keys:
            _FAILURES.setdefault(key, []).append(now or time.time())


def clear_failures(*keys: str) -> None:
    with _FAILURE_LOCK:
        for key in keys:
            _FAILURES.pop(key, None)


def login(email: str, password: str) -> str | None:
    """Session token for valid credentials, else ``None`` (same hashing work for unknown emails)."""
    known = accounts()
    email = normalize_email(email)
    stored = known.get(email) or _unknown_account_hash()
    if not verify_password(stored, password) or email not in known:
        return None
    return issue_session(email)
