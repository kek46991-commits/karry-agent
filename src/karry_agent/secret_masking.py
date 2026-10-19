"""Mask credentials before they reach the UI, API responses or stored traces.

Masked values keep a short recognisable prefix and the last four characters
(``sk-...a1b2``) so an owner can tell which key was involved without exposing it.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from .self_healing import _SECRET_KEY

# Personal context is private, but it is not a credential; the agent must keep using it.
_PERSONAL_KEYS = {"PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT"}
_MASK = "[REDACTED]"

_TOKEN_PATTERNS = [
    re.compile(r"\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_-]{30,}"),
    re.compile(r"\b(github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(gh[pousr]_)[A-Za-z0-9]{20,}"),
    re.compile(r"\b(glpat-)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(xox[abpr]-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(rnd_)[A-Za-z0-9]{16,}"),
    re.compile(r"\b(r8_)[A-Za-z0-9]{16,}"),
    re.compile(r"\b(AKIA)[0-9A-Z]{16}\b"),
    re.compile(r"\b(ya29\.)[A-Za-z0-9_-]{20,}"),
]
_DSN = re.compile(r"\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqps?)://)[^\s/@:]+:[^\s/@]+@", re.I)
_BEARER = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)


def mask_token(value: str, prefix: str = "") -> str:
    """``sk-abcdef...wxyz`` -> ``sk-...wxyz``; short values are fully hidden."""
    if len(value) < 16:
        return _MASK
    return f"{prefix or value[:3]}...{value[-4:]}"


def _configured_secrets() -> list[str]:
    values = []
    for key, value in os.environ.items():
        value = value.strip()
        if key in _PERSONAL_KEYS or not value or not _SECRET_KEY.search(key) or value in {"0", "1", "true", "false"}:
            continue
        values.append(value)
    return sorted(set(values), key=len, reverse=True)


def mask_text(text: str) -> str:
    if not text:
        return text
    for secret in _configured_secrets():
        if len(secret) >= 8:
            text = text.replace(secret, mask_token(secret))
        else:
            text = re.sub(r"(?<![\w-])" + re.escape(secret) + r"(?![\w-])", _MASK, text)
    text = _PRIVATE_KEY.sub("-----PRIVATE KEY [REDACTED]-----", text)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: mask_token(m.group(0), m.group(1)), text)
    text = _DSN.sub(r"\1[REDACTED]@", text)
    return _BEARER.sub(lambda m: m.group(1) + mask_token(m.group(2)), text)


def mask_secrets(value: Any, depth: int = 0) -> Any:
    """Recursively mask strings in JSON-like data. Keys that name a credential are hidden entirely."""
    if depth > 20:
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {k: (_MASK if isinstance(v, str) and v and _is_secret_field(str(k)) else mask_secrets(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(mask_secrets(v, depth + 1) for v in value)
    return value


_SECRET_FIELD = re.compile(r"(?:^|_)(?:api_?key|access_?key|secret(?:_access_key)?|password|passphrase|token|"
                           r"authorization|cookie|database_url|dsn|private_key|client_secret|refresh_token)$", re.I)
_PUBLIC_FIELDS = {"tokens", "total_tokens", "input_tokens", "output_tokens", "max_tokens", "max_output_tokens",
                  "page_token", "next_page_token"}


def _is_secret_field(name: str) -> bool:
    return name.lower() not in _PUBLIC_FIELDS and bool(_SECRET_FIELD.search(name.replace("-", "_")))
