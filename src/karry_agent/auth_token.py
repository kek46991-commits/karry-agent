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
