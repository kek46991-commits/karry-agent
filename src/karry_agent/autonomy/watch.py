"""Hooks for a separate monitoring device (見守り端末). The monitor itself is not part of this app.

* ``AuditLog``: append-only, hash-chained JSONL of every planned/started/finished subtask and verdict.
  A monitor can pull it (``GET /api/autonomy/runs/{id}/audit``) and check it with ``verify()``.
  With ``KARRY_MONITOR_SECRET`` set, every entry also carries an HMAC so the monitor can verify it.
* ``MonitorClient``: pushes each audit entry to ``KARRY_MONITOR_URL``. In ``enforce`` mode it also asks
  the monitor for a verdict before every subtask.
* ``MonitorGate``: decides allow / deny / hold for one subtask. It checks inbound monitor signals
  (pause/stop), then a policy file the monitor can replace (``KARRY_MONITOR_POLICY_FILE``), then
  the monitor's verdict. A configured check that cannot be read or reached holds the run (fail closed).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import requests

from ..secret_masking import mask_secrets
from .state import RunState, Subtask

logger = logging.getLogger(__name__)

MODES = ("off", "log", "enforce")
MAX_POLICY_BYTES = 256 * 1024
MAX_AUDIT_ENTRIES = 5000
GENESIS = "0" * 64


@dataclass(frozen=True)
class Verdict:
    decision: Literal["allow", "deny", "hold"]
    reason: str = ""
    source: str = "default"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuditLog:
    """Hash chain: ``hash = sha256(prev_hash + canonical(entry without hash/sig))``."""

    def __init__(self, run_id: str, path: Path | None = None, secret: str | None = None):
        self.run_id = run_id
        self.path = path
        self.secret = (secret if secret is not None else os.getenv("KARRY_MONITOR_SECRET", "")).strip()
        self._memory: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def entries(self, after: int = 0) -> list[dict[str, Any]]:
        if self.path is None:
            with self._lock:
                rows = list(self._memory)
        else:
            try:
                with self.path.open(encoding="utf-8") as handle:
                    rows = [json.loads(line) for line in handle if line.strip()]
            except FileNotFoundError:
                rows = []
        return [row for row in rows if row.get("seq", 0) > after]

    def _sign(self, digest: str) -> str:
        return hmac.new(self.secret.encode(), digest.encode(), hashlib.sha256).hexdigest()

    def append(self, event: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            rows = self._memory if self.path is None else None
            last = rows[-1] if rows else (self._last_file_entry() if self.path is not None else None)
            seq = (last["seq"] if last else 0) + 1
            if seq > MAX_AUDIT_ENTRIES:
                raise RuntimeError("audit log is full")
            body = {"seq": seq, "ts": round(time.time(), 3), "run_id": self.run_id, "event": event,
                    "data": mask_secrets(data or {}), "prev": last["hash"] if last else GENESIS}
            digest = hashlib.sha256((body["prev"] + _canonical(body)).encode()).hexdigest()
            entry = {**body, "hash": digest, **({"sig": self._sign(digest)} if self.secret else {})}
            if self.path is None:
                self._memory.append(entry)
            else:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(_canonical(entry) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            return entry

    def _last_file_entry(self) -> dict[str, Any] | None:
        rows = self.entries()
        return rows[-1] if rows else None

    def verify(self) -> bool:
        prev = GENESIS
        for index, entry in enumerate(self.entries(), start=1):
            body = {k: entry.get(k) for k in ("seq", "ts", "run_id", "event", "data", "prev")}
            digest = hashlib.sha256((prev + _canonical(body)).encode()).hexdigest()
            if entry.get("seq") != index or entry.get("prev") != prev or entry.get("hash") != digest:
                return False
            if self.secret and not hmac.compare_digest(str(entry.get("sig", "")), self._sign(digest)):
                return False
            prev = digest
        return True


def _monitor_url_ok(url: str) -> bool:
    parts = urlsplit(url)
    if parts.username or parts.password or not parts.hostname:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})


class MonitorClient:
    """HTTP link to the monitor: ``POST {url}/events`` (fire-and-forget) and ``POST {url}/review`` (verdict)."""

    def __init__(self, url: str, secret: str = "", timeout: float = 5.0, session: Any = None):
        if not _monitor_url_ok(url):
            raise ValueError("KARRY_MONITOR_URL は https（ローカル開発のみ http://localhost）で指定してください。")
        self.url = url.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self.session = session or requests.Session()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        body = _canonical(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Karry-Signature"] = "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        response = self.session.post(self.url + path, data=body, headers=headers, timeout=self.timeout, allow_redirects=False)
        response.raise_for_status()
        return response

    def send(self, entry: dict[str, Any]) -> None:
        try:
            self._post("/events", entry)
        except Exception as exc:  # noqa: BLE001 - the monitor can still pull the audit log
            logger.warning("monitor event delivery failed: %s", type(exc).__name__)

    def review(self, payload: dict[str, Any]) -> Verdict:
        try:
            data = self._post("/review", payload).json()
            decision = data.get("verdict") if isinstance(data, dict) else None
            if decision not in {"allow", "deny", "hold"}:
                return Verdict("hold", "見守り端末の応答形式が不正です。", "monitor")
            return Verdict(decision, str(data.get("reason", ""))[:500], "monitor")
        except Exception as exc:  # noqa: BLE001 - fail closed: no verdict means no execution
            logger.warning("monitor review failed: %s", type(exc).__name__)
            return Verdict("hold", "見守り端末に接続できないため保留しました。", "monitor")


class MonitorGate:
    def __init__(self, mode: str = "off", client: MonitorClient | None = None, policy_file: str | os.PathLike[str] | None = None,
                 config_error: str = ""):
        if mode not in MODES:
            raise ValueError(f"KARRY_MONITOR_MODE は {', '.join(MODES)} のいずれかです。")
        self.mode = mode
        self.client = client
        self.policy_file = Path(policy_file) if policy_file else None
        self.config_error = config_error

    @classmethod
    def from_env(cls) -> MonitorGate:
        url = os.getenv("KARRY_MONITOR_URL", "").strip()
        mode = os.getenv("KARRY_MONITOR_MODE", "").strip().lower() or ("log" if url else "off")
        policy = os.getenv("KARRY_MONITOR_POLICY_FILE", "").strip() or None
        try:
            client = MonitorClient(url, os.getenv("KARRY_MONITOR_SECRET", "").strip()) if url else None
            return cls(mode, client, policy)
        except ValueError as exc:
            # A broken monitor setting must not silently disable monitoring.
            return cls("enforce", None, policy, config_error=str(exc))

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "monitor_url": bool(self.client), "policy_file": bool(self.policy_file),
                "signed_audit": bool(os.getenv("KARRY_MONITOR_SECRET", "").strip()),
                "inbound_token": bool(os.getenv("KARRY_MONITOR_TOKEN", "").strip()),
                "config_error": self.config_error}

    def publish(self, entry: dict[str, Any]) -> None:
        if self.client is not None and self.mode in {"log", "enforce"}:
            self.client.send(entry)

    def _policy(self) -> dict[str, Any] | Verdict:
        assert self.policy_file is not None
        try:
            if self.policy_file.stat().st_size > MAX_POLICY_BYTES:
                raise ValueError("too large")
            data = json.loads(self.policy_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("object required")
            return data
        except (OSError, ValueError):
            return Verdict("hold", "検閲ファイルを読み込めないため保留しました。", "policy")

    def _check_policy(self, subtask: Subtask) -> Verdict | None:
        policy = self._policy()
        if isinstance(policy, Verdict):
            return policy
        if policy.get("halt"):
            return Verdict("hold", str(policy.get("reason") or "検閲ファイルで一時停止中です。")[:500], "policy")
        if subtask.kind in set(policy.get("blocked_kinds") or []):
            return Verdict("deny", f"検閲ファイルで「{subtask.kind}」の実行が禁止されています。", "policy")
        try:
            for pattern in policy.get("blocked_patterns") or []:
                if re.search(str(pattern), subtask.prompt, re.IGNORECASE):
                    return Verdict("deny", "検閲ファイルの禁止パターンに一致しました。", "policy")
        except re.error:
            return Verdict("hold", "検閲ファイルの禁止パターンが不正なため保留しました。", "policy")
        return None

    def review(self, state: RunState, subtask: Subtask) -> Verdict:
        if state.control == "stop":
            return Verdict("deny", state.control_reason or "見守り端末が停止を指示しました。", "signal")
        if state.control == "pause":
            return Verdict("hold", state.control_reason or "見守り端末が一時停止を指示しました。", "signal")
        if self.config_error:
            return Verdict("hold", f"見守り設定が不正なため保留しました: {self.config_error}", "config")
        if self.policy_file is not None:
            verdict = self._check_policy(subtask)
            if verdict is not None:
                return verdict
        if self.mode == "enforce":
            if self.client is None:
                return Verdict("hold", "KARRY_MONITOR_MODE=enforce ですが KARRY_MONITOR_URL が未設定です。", "config")
            return self.client.review(mask_secrets({"run_id": state.id, "goal": state.goal, "subtask": {
                "id": subtask.id, "kind": subtask.kind, "prompt": subtask.prompt, "attempt": subtask.attempts + 1}}))
        return Verdict("allow")
