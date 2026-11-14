"""Company-managed devices paired once with a shared enrollment password.

``worker`` devices (the phone doing the work) send heartbeats and, only while the user explicitly shares
their screen, a low-resolution preview frame. ``monitor`` devices read everything and can pause/stop runs.
Device tokens are random, shown to the device once (HttpOnly cookie) and stored only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROLES = ("worker", "monitor")
MAX_DEVICES = 20
MAX_FRAME_BYTES = 600 * 1024
ONLINE_SECONDS = 45
FRAME_STALE_SECONDS = 30
STATUS_KEYS = {"visible": bool, "sharing": bool, "battery": (int, float), "charging": bool, "app": str, "note": str,
               "platform": str, "run_id": str}
FRAME_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def frame_type(data: bytes) -> str | None:
    for magic, mime in FRAME_TYPES.items():
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clean_status(raw: dict[str, Any]) -> dict[str, Any]:
    status: dict[str, Any] = {}
    for key, kind in STATUS_KEYS.items():
        value = raw.get(key)
        if isinstance(value, kind) and not (kind is not bool and isinstance(value, bool)):
            status[key] = value[:120] if isinstance(value, str) else value
    if "battery" in status:
        status["battery"] = max(0, min(100, round(float(status["battery"]))))
    return status


@dataclass
class Device:
    id: str
    role: str
    name: str
    token_hash: str
    created_at: float = field(default_factory=time.time)
    last_seen: float = 0.0
    status: dict[str, Any] = field(default_factory=dict)

    def public(self, now: float | None = None, frame_at: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        sharing = bool(self.status.get("sharing")) and frame_at is not None and now - frame_at < FRAME_STALE_SECONDS
        return {"id": self.id, "role": self.role, "name": self.name, "created_at": self.created_at, "last_seen": self.last_seen,
                "online": bool(self.last_seen) and now - self.last_seen < ONLINE_SECONDS, "status": self.status,
                "sharing": sharing, "frame_at": frame_at}


class DeviceError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class DeviceRegistry:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = {}
        self._frames: dict[str, tuple[bytes, str, float]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._devices = {d["id"]: Device(**{k: d[k] for k in Device.__dataclass_fields__ if k in d}) for d in data.get("devices", [])}

    def _persist(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"devices": [asdict(d) for d in self._devices.values()]}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def _event(self, device: Device, event: str, **data: Any) -> None:
        self.events.append({"ts": time.time(), "device_id": device.id, "name": device.name, "role": device.role, "event": event, **data})

    def enroll(self, role: str, name: str) -> tuple[Device, str]:
        if role not in ROLES:
            raise DeviceError("role は worker か monitor です。", "invalid")
        with self._lock:
            if len(self._devices) >= MAX_DEVICES:
                raise DeviceError("登録できる端末数の上限に達しました。不要な端末を解除してください。", "capacity")
            token = secrets.token_urlsafe(32)
            device = Device(secrets.token_hex(8), role, name.strip()[:40] or ("見守り端末" if role == "monitor" else "作業端末"), _hash(token))
            self._devices[device.id] = device
            self._persist()
            self._event(device, "enrolled")
            return device, token

    def authenticate(self, token: str | None) -> Device | None:
        if not token or len(token) > 200:
            return None
        digest = _hash(token)
        with self._lock:
            for device in self._devices.values():
                if secrets.compare_digest(device.token_hash, digest):
                    return device
        return None

    def heartbeat(self, device: Device, raw: dict[str, Any]) -> Device:
        status = clean_status(raw)
        with self._lock:
            was_sharing = bool(device.status.get("sharing"))
            online = bool(device.last_seen) and time.time() - device.last_seen < ONLINE_SECONDS
            device.status, device.last_seen = status, time.time()
            if not online:
                self._event(device, "online")
            if was_sharing != bool(status.get("sharing")):
                self._event(device, "sharing_started" if status.get("sharing") else "sharing_stopped")
                if not status.get("sharing"):
                    self._frames.pop(device.id, None)
            self._persist()
        return device

    def put_frame(self, device: Device, data: bytes) -> None:
        if device.role != "worker":
            raise DeviceError("画面を送れるのは作業端末だけです。", "forbidden")
        if not device.status.get("sharing"):
            raise DeviceError("画面共有を開始してから送ってください。", "not_sharing")
        if len(data) > MAX_FRAME_BYTES:
            raise DeviceError("画像が大きすぎます。", "too_large")
        mime = frame_type(data)
        if mime is None:
            raise DeviceError("JPEG / PNG / WebP の画像だけ送れます。", "invalid")
        with self._lock:
            self._frames[device.id] = (data, mime, time.time())

    def frame(self, device_id: str) -> tuple[bytes, str, float] | None:
        with self._lock:
            return self._frames.get(device_id)

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [d.public(now, (self._frames.get(d.id) or (b"", "", None))[2]) for d in
                    sorted(self._devices.values(), key=lambda d: d.created_at)]

    def revoke(self, device_id: str, by: Device | None = None) -> None:
        with self._lock:
            device = self._devices.pop(device_id, None)
            if device is None:
                raise DeviceError("端末が見つかりません。", "not_found")
            self._frames.pop(device_id, None)
            self._persist()
            self._event(device, "revoked", by=by.name if by else "owner")
