"""Central, redacted activity feed and fixed controls for the owner's monitor dashboard.

Every HTTP request, agent run, tool call, clone run, mobile action and warning log is
published to one in-memory hub. Any monitor device reads the same hub, so they all see
the same state. Payloads pass through ``redact``/``mask_secrets`` before they are stored,
secret-chat traffic is recorded without bodies, and the only controls are pause/resume.
The hub lives in the server process, so it is reset when the server restarts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse as StarletteJSONResponse

from . import secret_runtime
from .secret_masking import mask_secrets
from .self_healing import redact

NO_STORE = {"Cache-Control": "private, no-store"}
MAX_EVENTS = 2000
MAX_SUMMARY = 300
MAX_TEXT = 2000
MAX_BODY_BYTES = 4096
STREAM_SECONDS = 300
KEEPALIVE_SECONDS = 15
MONITOR_PREFIX = "/api/monitor"
# POSTs that start agent work; refused with 423 while the monitor has paused execution.
EXECUTION_PATHS = ("/chat", "/run", "/agent/run", "/api/chat", "/api/agent/tools/execute",
                   "/api/optimus/clones")
MOBILE_PREFIX = "/api/mobile-agent"
# Bodies never recorded: credentials, uploaded media, chat text (agent events already log it
# while honouring private mode) and mobile payloads (message drafts, picked file names).
BODYLESS_PATHS = ("/api/login", "/api/media/upload", "/api/upload_image", "/api/secret", "/api/mobile-agent",
                  *secret_runtime.CHAT_PATHS)
OUTLINE_KEYS = {"status", "ok", "error", "error_type", "count", "path", "url", "id"}

Level = Literal["info", "warning", "error"]


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _starts_execution(path: str) -> bool:
    """Submitting, confirming and claiming mobile actions start work; reporting a claimed one does not."""
    if _under(path, MOBILE_PREFIX):
        return path == f"{MOBILE_PREFIX}/actions" or path.endswith(("/confirm", "/claim"))
    return any(_under(path, p) for p in EXECUTION_PATHS)


def _summary(text: Any) -> str:
    value = str(redact(mask_secrets(str(text))))
    return value if len(value) <= MAX_SUMMARY else value[: MAX_SUMMARY - 1] + "…"


def _bound(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[: MAX_TEXT - 1] + "…"
    if isinstance(value, dict):
        return {key: _bound(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bound(item) for item in value]
    return value


def outline(result: Any) -> Any:
    """Describe a tool result without copying its content (file text, messages, media)."""
    if isinstance(result, dict):
        return {key: (_summary(value) if key in OUTLINE_KEYS and isinstance(value, (str, int, float, bool))
                      else f"[{type(value).__name__}]") for key, value in result.items()}
    if isinstance(result, (list, tuple)):
        return f"[{len(result)} items]"
    if isinstance(result, str):
        return f"[text, {len(result)} chars]"
    return f"[{type(result).__name__}]"


def safe_data(value: Any) -> Any:
    return _bound(redact(mask_secrets(value)))


class MonitorHub:
    def __init__(self, max_events: int = MAX_EVENTS, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._changed = threading.Condition()
        self._providers: dict[str, Callable[[], Any]] = {}
        self.reset()

    def reset(self) -> None:
        with self._changed:
            self._events.clear()
            self._seq = 0
            self._counts: Counter[str] = Counter()
            self._levels: Counter[str] = Counter()
            self._active_requests = 0
            self._paused = False
            self._pause_reason = ""
            self._last_error: dict[str, Any] | None = None
            self._started_at = self._clock()

    def publish(self, kind: str, summary: str, data: Any = None, *, level: Level = "info",
                source: str = "backend") -> dict[str, Any]:
        if secret_runtime.is_active():
            data = None
            summary = "シークレット処理（内容は記録しません）"
        event = {"kind": kind[:40], "source": source[:60], "level": level, "summary": _summary(summary),
                 "data": safe_data(data) if data is not None else None}
        with self._changed:
            self._seq += 1
            event = {"seq": self._seq, "at": self._clock(), **event}
            self._events.append(event)
            self._counts[event["kind"]] += 1
            self._levels[level] += 1
            if level == "error":
                self._last_error = event
            self._changed.notify_all()
        return event

    def events(self, after: int = 0, limit: int = 200, kinds: set[str] | None = None) -> list[dict[str, Any]]:
        with self._changed:
            rows = [e for e in self._events if e["seq"] > after and (not kinds or e["kind"] in kinds)]
        return rows[:max(1, min(limit, 500))]

    def wait(self, after: int, timeout: float) -> list[dict[str, Any]]:
        with self._changed:
            self._changed.wait_for(lambda: self._seq > after, timeout=timeout)
        return self.events(after, limit=500)

    def request_started(self) -> None:
        with self._changed:
            self._active_requests += 1

    def request_finished(self) -> None:
        with self._changed:
            self._active_requests -= 1

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool, reason: str = "") -> dict[str, Any]:
        with self._changed:
            self._paused = paused
            self._pause_reason = _summary(reason) if paused else ""
        self.publish("control", "実行を一時停止しました" if paused else "実行を再開しました",
                     {"paused": paused, "reason": self._pause_reason}, level="warning" if paused else "info",
                     source="monitor")
        return self.state()

    def register_state(self, name: str, provider: Callable[[], Any]) -> None:
        self._providers[name] = provider

    def state(self) -> dict[str, Any]:
        with self._changed:
            snapshot = {
                "latest_seq": self._seq,
                "started_at": self._started_at,
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "active_requests": self._active_requests,
                "counts": dict(self._counts),
                "levels": dict(self._levels),
                "last_error": self._last_error,
            }
        components = {}
        for name, provider in self._providers.items():
            try:
                components[name] = safe_data(provider())
            except Exception as exc:
                components[name] = {"error": type(exc).__name__}
        return {**snapshot, "components": components}


hub = MonitorHub()


def _auth_kind(headers: dict[bytes, bytes]) -> str:
    if b"authorization" in headers:
        return "owner"
    if b"x-device-token" in headers:
        return "device"
    return "none"


def _body(raw: bytes, total: int, content_type: str) -> Any:
    if not total:
        return None
    if "json" not in content_type and "event-stream" not in content_type and "text/plain" not in content_type:
        return f"[{total} bytes, {content_type or 'unknown'}]"
    text = raw.decode("utf-8", "replace")
    if "json" in content_type and total <= MAX_BODY_BYTES:
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text + ("…" if total > len(raw) else "")


class MonitorMiddleware:
    """Record each HTTP exchange (bounded, redacted) and enforce the monitor's pause switch."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or _under(path, MONITOR_PREFIX) or path.startswith("/static/"):
            return await self.app(scope, receive, send)
        method = scope.get("method", "")
        headers = dict(scope.get("headers") or [])
        if hub.paused and method == "POST" and _starts_execution(path):
            hub.publish("http", f"{method} {path} → 423（一時停止中）", {"method": method, "path": path,
                        "status": 423, "auth": _auth_kind(headers)}, level="warning")
            return await StarletteJSONResponse({"detail": "監視側で実行を一時停止しています。"}, status_code=423,
                                               headers=NO_STORE)(scope, receive, send)
        request_raw, response_raw = bytearray(), bytearray()
        sizes = {"request": 0, "response": 0}
        response = {"status": 500, "content_type": ""}

        async def recording_receive():
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                sizes["request"] += len(chunk)
                request_raw.extend(chunk[: max(0, MAX_BODY_BYTES - len(request_raw))])
            return message

        async def recording_send(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                response["content_type"] = dict(message.get("headers") or []).get(b"content-type", b"").decode("latin-1")
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                sizes["response"] += len(chunk)
                response_raw.extend(chunk[: max(0, MAX_BODY_BYTES - len(response_raw))])
            await send(message)

        started = time.perf_counter()
        hub.request_started()
        failure = None
        try:
            await self.app(scope, recording_receive, recording_send)
        except Exception as exc:
            failure = exc
            raise
        finally:
            hub.request_finished()
            status = response["status"]
            private = bool(scope.get("secret_chat")) or any(_under(path, p) for p in BODYLESS_PATHS)
            data = {
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "auth": _auth_kind(headers),
                "request_bytes": sizes["request"],
                "response_bytes": sizes["response"],
                "request_body": None if private else _body(bytes(request_raw), sizes["request"],
                                                           headers.get(b"content-type", b"").decode("latin-1")),
                "response_body": None if private else _body(bytes(response_raw), sizes["response"],
                                                            response["content_type"]),
                "private": private,
            }
            if failure is not None:
                data["exception"] = type(failure).__name__
            level: Level = "error" if failure is not None or status >= 500 else "warning" if status >= 400 else "info"
            hub.publish("http", f"{method} {path} → {status}", data, level=level)


class MonitorLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: Level = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
            data = {"logger": record.name, "level": record.levelname}
            if record.exc_info and record.exc_info[0] is not None:
                data["exception"] = record.exc_info[0].__name__
            hub.publish("log", record.getMessage(), data, level=level, source=record.name)
        except Exception:
            self.handleError(record)


_OWN_LOGS = MonitorLogHandler(logging.INFO)
_LIBRARY_WARNINGS = MonitorLogHandler(logging.WARNING)
_LIBRARY_WARNINGS.addFilter(lambda record: not record.name.startswith("karry_agent"))


def install_log_capture() -> None:
    """Capture INFO logs from karry_agent itself and warnings from every other library."""
    own = logging.getLogger("karry_agent")
    if _OWN_LOGS not in own.handlers:
        own.addHandler(_OWN_LOGS)
    if own.level == logging.NOTSET or own.level > logging.INFO:
        own.setLevel(logging.INFO)
    root = logging.getLogger()
    if _LIBRARY_WARNINGS not in root.handlers:
        root.addHandler(_LIBRARY_WARNINGS)


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "resume"]
    reason: str = Field(default="", max_length=200)


def _sse(event: str, data: Any, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def stream(after: int, seconds: float) -> Iterator[str]:
    yield _sse("state", hub.state())
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        events = hub.wait(after, min(KEEPALIVE_SECONDS, remaining))
        if not events:
            yield ": keepalive\n\n"
            continue
        for event in events:
            after = event["seq"]
            yield _sse("activity", event, after)
    yield _sse("reconnect", {"after": after})


def build_router(require_auth: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix=MONITOR_PREFIX, tags=["monitor"], dependencies=[Depends(require_auth)])

    @router.get("/state")
    def monitor_state() -> JSONResponse:
        return JSONResponse(hub.state(), headers=NO_STORE)

    @router.get("/events")
    def monitor_events(
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        kind: Annotated[list[str] | None, Query(max_length=40)] = None,
    ) -> JSONResponse:
        events = hub.events(after, limit, set(kind) if kind else None)
        return JSONResponse({"events": events, "latest_seq": hub.state()["latest_seq"]}, headers=NO_STORE)

    @router.get("/stream")
    def monitor_stream(
        after: Annotated[int, Query(ge=0)] = 0,
        seconds: Annotated[float, Query(gt=0, le=STREAM_SECONDS)] = STREAM_SECONDS,
    ) -> StreamingResponse:
        return StreamingResponse(stream(after, seconds), media_type="text/event-stream",
                                 headers={**NO_STORE, "X-Accel-Buffering": "no"})

    @router.post("/control")
    def monitor_control(body: ControlRequest) -> JSONResponse:
        return JSONResponse(hub.set_paused(body.action == "pause", body.reason), headers=NO_STORE)

    return router


def page_response(path: Any) -> FileResponse:
    return FileResponse(path, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
                                       "Referrer-Policy": "no-referrer"})
