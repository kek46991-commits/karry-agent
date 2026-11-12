"""Owner-token normalization: stray spaces/newlines around or inside the token never break login."""
from __future__ import annotations

import os
import secrets


def normalize_token(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("latin1")
    return "".join(str(value or "").split())


def configured_token() -> str:
    return normalize_token(os.getenv("KARRY_API_TOKEN"))


def token_matches(supplied: str | bytes | None, expected: str | None = None) -> bool:
    expected = configured_token() if expected is None else normalize_token(expected)
    supplied = normalize_token(supplied)
    return bool(expected) and bool(supplied) and secrets.compare_digest(supplied.encode(), expected.encode())


def owner_credential_ok(supplied: str | bytes | None, expected: str | None = None) -> bool:
    """The owner token itself, or a session issued by an email + password login (``/api/login``)."""
    from karry_agent import user_login

    if isinstance(supplied, bytes):
        supplied = supplied.decode("latin1")
    return token_matches(supplied, expected) or user_login.session_email(supplied) is not None
