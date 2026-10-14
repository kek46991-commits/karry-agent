"""Shared, bounded self-healing pipeline. A diagnosis is never a claimed repair."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
_SECRET_KEY = re.compile(r"token|secret|password|authorization|cookie|api.?key|access[_-]?key|credential|passphrase|database_url|postgres_url|dsn|partner_name|core_user_name|core_private_context", re.I)


def redact(value: Any, depth: int = 0) -> Any:
    """Bound diagnostics; strip secrets, URLs' query strings and arbitrary objects."""
    if depth > 5:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {str(k)[:100]: "[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, depth + 1)
                for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value[:30]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = value if isinstance(value, str) else type(value).__name__
    # Redact exact configured secrets before truncation, including the short owner token.
    for key, secret in os.environ.items():
        if _SECRET_KEY.search(key) and secret:
            text = text.replace(secret, "[redacted]")
            if key in {"PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT"} and secret.strip():
                text = text.replace(secret.strip(), "[redacted]")
    text = re.sub(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]*", r"\1[redacted]", text)
    text = re.sub(r"(?i)bearer\s+[^\s\"',;}]+", "Bearer [redacted]", text)
    text = re.sub(
        r'''(?i)(["']?[\w-]*(?:token|secret|password|passphrase|api[_-]?key|access[_-]?key|credential|authorization|cookie|partner_name|core_user_name|core_private_context)[\w-]*["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)''',
        r"\1[redacted]", text)
    text = re.sub(r"((?:https?|postgres(?:ql)?)://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    return text[:6000]


@dataclass
class RepairStrategy:
    name: str
    matches: Callable[[Exception, dict[str, Any]], bool]
    apply: Callable[[Exception, dict[str, Any]], Any]
    verify: Callable[[Any, dict[str, Any]], bool]
    rollback: Callable[[Any, dict[str, Any]], None] | None = None


class SelfHealingEngine:
    """Capture → diagnose → apply an owned strategy → verify → report or escalate."""

    def __init__(self, max_logs: int = 100, cooldown: float = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._logs: deque[dict[str, Any]] = deque(maxlen=max(1, min(max_logs, 1000)))
        self._recent: dict[str, tuple[float, str]] = {}
        self._lock = threading.RLock()
        self._local = threading.local()
        self._strategies: list[RepairStrategy] = []
        self._patch_hook: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.cooldown = max(0, cooldown)
        self.clock = clock

    @property
    def error_logs(self) -> list[dict[str, Any]]:
        with self._lock:
            # Return detached diagnostics; callers cannot mutate the internal records.
            import copy
            return copy.deepcopy(list(self._logs))

    def register(self, strategy: RepairStrategy) -> None:
        with self._lock:
            self._strategies = [s for s in self._strategies if s.name != strategy.name] + [strategy]

    def set_patch_hook(self, hook: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._patch_hook = hook

    def classify(self, error: Exception, context: dict[str, Any]) -> str:
        status = getattr(error, "status_code", None) or getattr(error, "code", None) or context.get("status_code")
        response = getattr(error, "response", None)
        if response is not None:
            status = getattr(response, "status_code", status)
        text = str(error).lower()
        if status == 401 or "bearer token" in text:
            return "authentication"
        if status == 429:
            return "rate_limit"
        if status in (500, 502, 503, 504) or isinstance(error, (TimeoutError, ConnectionError)):
            return "transient"
        if isinstance(error, (ValueError, TypeError)):
            return "configuration_or_input"
        return "code_or_dependency"

    def _finish(self, incident: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        safe = redact({"incident_id": incident["incident_id"], **result})
        with self._lock:
            if safe.get("status") == "patch_queued" and incident.get("healing_status", {}).get("status") in {
                "patch_ready", "pr_created", "patch_failed"
            }:
                return incident["healing_status"]
            incident["healing_status"] = safe
        return safe

    def update(self, incident_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            for incident in self._logs:
                if incident["incident_id"] == incident_id:
                    self._finish(incident, result)
                    break

    def analyze_and_heal(self, error: Exception, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        if getattr(self._local, "active", False):
            return {"status": "skipped", "reason": "recursive_healing_suppressed"}
        self._local.active = True
        try:
            # TracebackException avoids source lines/locals which may contain private prompts.
            trace = [{"file": os.path.basename(f.filename), "line": f.lineno, "function": f.name}
                     for f in traceback.extract_tb(error.__traceback__)[-20:]]
            safe_context = redact({k: v for k, v in context.items()
                                   if k in {"source", "operation", "method", "path", "run_id", "tool_name",
                                            "direction", "status_code", "attempt", "will_retry"}})
            category = self.classify(error, context)
            if context.get("source") in {"api_validation", "agent_configuration", "repair_configuration"}:
                category = "configuration_or_input"
            fingerprint = hashlib.sha256(repr((type(error).__name__, category, safe_context.get("source"),
                                               safe_context.get("operation"), safe_context.get("tool_name"), trace)).encode()).hexdigest()
            now = self.clock()
            with self._lock:
                previous = self._recent.get(fingerprint)
                if previous and now - previous[0] < self.cooldown:
                    return {"status": "suppressed", "incident_id": previous[1], "reason": "duplicate_cooldown"}
                incident = {"incident_id": uuid.uuid4().hex, "error_type": type(error).__name__,
                            "error": redact(str(error)), "trace": trace, "context": safe_context,
                            "category": category, "recorded_at": time.time()}
                self._logs.append(incident)
                self._recent[fingerprint] = (now, incident["incident_id"])
                if len(self._recent) > self._logs.maxlen:
                    oldest = min(self._recent, key=lambda key: self._recent[key][0])
                    self._recent.pop(oldest, None)
                strategies = list(self._strategies)
            logger.error("[Self-Healing Core Active] incident=%s category=%s type=%s",
                         incident["incident_id"], category, type(error).__name__)
            if category == "authentication":
                return self._finish(incident, self.fix_authentication_header(context))
            for strategy in strategies:
                state = None
                applied = False
                try:
                    if not strategy.matches(error, context):
                        continue
                    state = strategy.apply(error, context)
                    applied = True
                    if strategy.verify(state, context):
                        return self._finish(incident, {"status": "repaired", "action": strategy.name,
                                                       "verified": True})
                    failure = "verification_failed"
                except Exception:
                    failure = "strategy_failed"
                try:
                    if strategy.rollback and applied:
                        strategy.rollback(state, context)
                except Exception:
                    failure = "rollback_failed"
                return self._finish(incident, {"status": "repair_failed", "action": strategy.name,
                                               "reason": failure, "verified": False})
            if context.get("will_retry"):
                return self._finish(incident, {"status": "retry_scheduled", "category": category, "verified": False})
            if category == "code_or_dependency":
                return self._finish(incident, self.generate_repair_patch(incident))
            return self._finish(incident, {"status": "diagnosed", "category": category,
                                          "action": "inspect_configuration_or_provider", "verified": False})
        except Exception:
            # Healing must never replace the original error with a second failure.
            return {"status": "healing_failed", "verified": False}
        finally:
            self._local.active = False

    def fix_authentication_header(self, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("direction") != "outbound":
            return {"status": "diagnosed", "action": "client_must_supply_valid_bearer_token",
                    "verified": False, "authentication_bypassed": False}
        headers = context.get("headers")
        provider = context.get("credential_provider")
        verifier = context.get("verify")
        if not isinstance(headers, dict) or not callable(provider) or not callable(verifier):
            return {"status": "diagnosed", "action": "outbound_auth_context_required", "verified": False}
        original = dict(headers)
        try:
            token = provider()
            if not isinstance(token, str) or not token.strip():
                return {"status": "diagnosed", "action": "credential_not_configured", "verified": False}
            for key in list(headers):
                if key.lower() == "authorization":
                    del headers[key]
            headers["Authorization"] = "Bearer " + token.strip()
            if verifier(headers) is True:
                return {"status": "repaired", "action": "applied_correct_bearer_header",
                        "verified": True}
        except Exception:
            pass
        headers.clear()
        headers.update(original)
        return {"status": "repair_failed", "action": "correct_bearer_header",
                "verified": False, "rolled_back": True}

    def generate_repair_patch(self, incident: dict[str, Any]) -> dict[str, Any]:
        if self._patch_hook is None:
            return {"status": "diagnosed", "action": "repair_patch_hook_not_enabled", "verified": False}
        return self._patch_hook(incident)

    def observe_result(self, result: Any, context: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(result, dict) and (result.get("error") or result.get("status") == "error"):
            # Structured tool failures have no traceback: do not fabricate one.
            message = str(result.get("error") or result.get("error_type") or "Tool error")
            known_input = re.search(r"required|missing|invalid argument|unknown tool|not configured|not found|must be|scope|oauth|認証|未設定",
                                   message + " " + str(result.get("error_type", "")), re.I)
            error = ValueError(message) if known_input else RuntimeError(message)
            return self.analyze_and_heal(error,
                                         {**context, "status_code": result.get("http_status")})
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": True, "incident_count": len(self._logs), "max_logs": self._logs.maxlen,
                    "strategies": [s.name for s in self._strategies], "patch_hook_enabled": self._patch_hook is not None}


healing_engine = SelfHealingEngine()


def workspace_directory_strategy() -> RepairStrategy:
    """Restore only the explicitly configured workspace, never arbitrary missing files."""
    from pathlib import Path

    def matches(error: Exception, context: dict[str, Any]) -> bool:
        configured = os.getenv("KARRY_WORKSPACE")
        filename = getattr(error, "filename", None)
        return bool(isinstance(error, FileNotFoundError) and configured and filename
                    and Path(filename).absolute() == Path(configured).absolute()
                    and not Path(configured).exists())

    def apply(error: Exception, context: dict[str, Any]) -> dict[str, Any]:
        target = Path(os.environ["KARRY_WORKSPACE"]).absolute()
        # No recursive creation: do not create an unexpected directory hierarchy.
        target.mkdir(mode=0o700)
        return {"path": target}

    def verify(state: dict[str, Any], context: dict[str, Any]) -> bool:
        return state["path"].is_dir() and os.access(state["path"], os.R_OK | os.W_OK)

    def rollback(state: dict[str, Any], context: dict[str, Any]) -> None:
        state["path"].rmdir()  # Refuses deletion if another operation has filled it.

    return RepairStrategy("restore_configured_workspace_directory", matches, apply, verify, rollback)


healing_engine.register(workspace_directory_strategy())
