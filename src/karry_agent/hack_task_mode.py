"""Request-scoped authorization for the dedicated ``ハック`` task."""
from __future__ import annotations

import contextlib
import contextvars
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

HACK_TASK_TITLE = "ハック"
PENTEST_TOOL_NAME = "run_authorized_pentest"

_URL_RE = re.compile(r"https?://[^\s<>{}\[\]\"']+")
_TRAILING_PUNCTUATION = ".,;:!?、。）」』】"
_PENTEST_INTENT_RE = re.compile(
    r"ペンテスト|ペネトレーション|脆弱性|攻撃テスト|セキュリティ(?:検査|診断)|"
    r"\bpentest\b|\bvulnerability\b|\bsecurity scan\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HackTaskContext:
    enabled: bool = False
    prompt: str = ""
    authorized_targets: frozenset[str] = frozenset()


_CONTEXT: contextvars.ContextVar[HackTaskContext] = contextvars.ContextVar(
    "karry_hack_task_context", default=HackTaskContext()
)


def is_hack_task(title: str | None) -> bool:
    """Only the exact task title enables the mode."""
    return title == HACK_TASK_TITLE


def normalize_target_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("対象は http(s)://host 形式のURLで指定してください。")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("ユーザー情報やフラグメントを含む対象URLは指定できません。")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def explicit_targets(prompt: str) -> frozenset[str]:
    targets: set[str] = set()
    for match in _URL_RE.findall(prompt):
        candidate = match.rstrip(_TRAILING_PUNCTUATION)
        try:
            targets.add(normalize_target_url(candidate))
        except ValueError:
            continue
    return frozenset(targets)


def requested_target(prompt: str) -> str | None:
    """Return the single explicit target for a pentest request, or no request."""
    if not _PENTEST_INTENT_RE.search(prompt):
        return None
    targets = explicit_targets(prompt)
    if not targets:
        raise ValueError("検証対象のURLを今回のメッセージ本文に明記してください。")
    if len(targets) != 1:
        raise ValueError("1回の検証で指定できる対象URLは1つです。")
    return next(iter(targets))


@contextlib.contextmanager
def scope(title: str | None, prompt: str):
    enabled = is_hack_task(title)
    token = _CONTEXT.set(HackTaskContext(
        enabled=enabled,
        prompt=prompt if enabled else "",
        authorized_targets=explicit_targets(prompt) if enabled else frozenset(),
    ))
    try:
        yield _CONTEXT.get()
    finally:
        _CONTEXT.reset(token)


def current() -> HackTaskContext:
    return _CONTEXT.get()


def authorize_target(target_url: str) -> str:
    context = current()
    if not context.enabled:
        raise PermissionError("このツールは題名が「ハック」のタスクでのみ利用できます。")
    normalized = normalize_target_url(target_url)
    if normalized not in context.authorized_targets:
        raise PermissionError("検証対象のURLを今回のメッセージ本文に明記してください。")
    return normalized
