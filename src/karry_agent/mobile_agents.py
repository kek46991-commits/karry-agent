"""Bounded event bridge for an owner-controlled mobile operation agent."""
from __future__ import annotations

import copy
import hashlib
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .location_history import RETENTION_DAYS, LocationHistoryError
from .location_history import store as location_history
from .self_healing import redact
from .url_analysis import URLFetchError, _resolve_public, validate_url

NO_STORE = {"Cache-Control": "private, no-store"}
MAX_EVENTS = 200
MAX_RESULT_TEXT = 2000
CLAIM_LEASE_SECONDS = 30
LOCATION_TTL_SECONDS = RETENTION_DAYS * 24 * 60 * 60

ActionName = Literal[
    "open_app",
    "open_url",
    "create_calendar_event",
    "create_reminder",
    "request_location",
    "choose_file",
    "choose_photo",
    "draft_message",
]

_CONFIRMATION_REQUIRED = frozenset({
    "open_url",
    "create_calendar_event",
    "create_reminder",
    "request_location",
    "draft_message",
})
_USER_PICKER_ACTIONS = frozenset({"choose_file", "choose_photo"})
_STATUS = frozenset({"awaiting_confirmation", "pending", "claimed", "completed", "failed"})
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BUNDLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{2,199}$")
_ACTION_FIELDS = {
    "open_app": frozenset({"bundle_id"}),
    "open_url": frozenset({"url"}),
    "create_calendar_event": frozenset({"title", "start_at", "end_at", "notes"}),
    "create_reminder": frozenset({"title", "due_at", "notes"}),
    "request_location": frozenset({"purpose"}),
    "choose_file": frozenset({"content_type"}),
    "choose_photo": frozenset({"selection_limit"}),
    "draft_message": frozenset({"recipient", "body"}),
}


class MobileAgentError(ValueError):
    pass


def _bounded_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    if len(value) > 20:
        raise MobileAgentError("payload may contain at most 20 fields")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if len(name) > 80:
            raise MobileAgentError("payload field names must be 80 characters or fewer")
        if isinstance(item, (str, int, float, bool)) or item is None:
            if isinstance(item, str) and len(item) > 1000:
                raise MobileAgentError("payload text values must be 1000 characters or fewer")
            safe[name] = item
        else:
            raise MobileAgentError("payload values must be scalar JSON values")
    return safe


def _validate_action_payload(action: ActionName, payload: dict[str, Any]) -> None:
    unexpected = set(payload) - _ACTION_FIELDS[action]
    if unexpected:
        raise MobileAgentError(f"unsupported payload fields for {action}: {', '.join(sorted(unexpected))}")
    if action == "open_app" and not _BUNDLE_ID.fullmatch(str(payload.get("bundle_id", ""))):
        raise MobileAgentError("open_app requires a valid bundle_id")
    if action == "open_url":
        url = str(payload.get("url", ""))
        try:
            scheme, host, port = validate_url(url)
            if scheme != "https":
                raise MobileAgentError("open_url requires a public https URL without embedded credentials")
            _resolve_public(host, port, 2.0)
        except URLFetchError as exc:
            raise MobileAgentError("open_url requires a public https URL without embedded credentials") from exc
        except (ValueError, UnicodeError):
            raise MobileAgentError("open_url requires a public https URL without embedded credentials")
    if action in {"create_calendar_event", "create_reminder"} and not str(payload.get("title", "")).strip():
        raise MobileAgentError(f"{action} requires a title")
    if action == "request_location" and not str(payload.get("purpose", "")).strip():
        raise MobileAgentError("request_location requires a visible purpose")
    if action == "choose_photo":
        selection_limit = payload.get("selection_limit", 1)
        if not isinstance(selection_limit, int) or isinstance(selection_limit, bool) or not 1 <= selection_limit <= 20:
            raise MobileAgentError("choose_photo selection_limit must be between 1 and 20")
    if action == "draft_message" and not str(payload.get("body", "")).strip():
        raise MobileAgentError("draft_message requires a body")


def _diagnose(error_code: str, message: str) -> dict[str, str]:
    code = error_code.lower()
    text = message.lower()
    if code in {"permission_denied", "authorization_denied"} or "permission" in text:
        category, recommendation = "permission", "request_the_required_ios_permission"
    elif code in {"app_unavailable", "intent_unavailable"} or "not installed" in text:
        category, recommendation = "capability", "verify_app_intent_or_shortcut_availability"
    elif code in {"timeout", "offline"} or "network" in text:
        category, recommendation = "transient", "retry_after_connectivity_recovers"
    elif code in {"invalid_payload", "validation"}:
        category, recommendation = "input", "correct_the_action_payload"
    else:
        category, recommendation = "execution", "review_the_redacted_device_log"
    return {"category": category, "recommendation": recommendation}


class MobileAgentCoordinator:
    """Separate operation events from redacted diagnosis, without device-wide access."""

    def __init__(self, max_events: int = MAX_EVENTS, clock: Callable[[], float] = time.time) -> None:
        self._events: dict[str, dict[str, Any]] = {}
        self._max_events = max(1, min(max_events, 1000))
        self._order: deque[str] = deque()
        self._device_tokens: dict[str, dict[str, str | None]] = {}
        self._locations: dict[str, list[dict[str, float]]] = {}
        self._lock = threading.RLock()
        self._clock = clock

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._order.clear()
            self._device_tokens.clear()
            self._locations.clear()

    def _check_device_id(self, device_id: str) -> None:
        if not _DEVICE_ID.fullmatch(device_id):
            raise MobileAgentError("device_id is invalid")

    def enroll(
        self,
        device_id: str,
        *,
        role: Literal["operation", "observer"] = "operation",
        observed_device_id: str | None = None,
    ) -> str:
        self._check_device_id(device_id)
        if role == "observer":
            if observed_device_id is None:
                raise MobileAgentError("observer enrollment requires observed_device_id")
            self._check_device_id(observed_device_id)
            if observed_device_id == device_id:
                raise MobileAgentError("observer and operation device IDs must be different")
        elif observed_device_id is not None:
            raise MobileAgentError("operation enrollment cannot set observed_device_id")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._device_tokens[device_id] = {
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "role": role,
                "observed_device_id": observed_device_id,
            }
        return token

    def authenticate_device(
        self,
        device_id: str,
        token: str,
        *,
        role: Literal["operation", "observer"],
        observed_device_id: str | None = None,
    ) -> None:
        self._check_device_id(device_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            registration = self._device_tokens.get(device_id)
        if registration is None or not secrets.compare_digest(digest, str(registration["token_hash"])):
            raise MobileAgentError("device authentication failed")
        if registration["role"] != role:
            raise MobileAgentError("device role is not authorized for this operation")
        if role == "observer" and registration["observed_device_id"] != observed_device_id:
            raise MobileAgentError("observer is not authorized for this operation device")

    def _copy(self, event: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(event)

    def _observer_copy(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": event["id"],
            "device_id": event["device_id"],
            "action": event["action"],
            "status": event["status"],
            "requires_confirmation": event["requires_confirmation"],
            "uses_system_picker": event["uses_system_picker"],
            "created_at": event["created_at"],
            "updated_at": event["updated_at"],
            "claimed_at": event["claimed_at"],
            "diagnostic": copy.deepcopy(event["diagnostic"]),
        }

    def _get(self, event_id: str, device_id: str) -> dict[str, Any]:
        event = self._events.get(event_id)
        if event is None or event["device_id"] != device_id:
            raise MobileAgentError("mobile action was not found")
        return event

    def submit(
        self,
        device_id: str,
        action: ActionName,
        payload: Mapping[str, Any],
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        self._check_device_id(device_id)
        safe_payload = _bounded_payload(payload)
        _validate_action_payload(action, safe_payload)
        now = self._clock()
        requires_confirmation = action in _CONFIRMATION_REQUIRED
        status = "pending" if confirmed or not requires_confirmation else "awaiting_confirmation"
        event = {
            "id": uuid.uuid4().hex,
            "device_id": device_id,
            "action": action,
            "payload": safe_payload,
            "status": status,
            "requires_confirmation": requires_confirmation,
            "uses_system_picker": action in _USER_PICKER_ACTIONS,
            "created_at": now,
            "updated_at": now,
            "claimed_at": None,
            "result": None,
            "diagnostic": None,
        }
        with self._lock:
            if len(self._order) >= self._max_events:
                removable = next(
                    (
                        event_id
                        for event_id in self._order
                        if self._events[event_id]["status"] in {"completed", "failed"}
                    ),
                    None,
                )
                if removable is None:
                    raise MobileAgentError("mobile action queue is full")
                self._order.remove(removable)
                self._events.pop(removable, None)
            self._events[event["id"]] = event
            self._order.append(event["id"])
        return self._copy(event)

    def confirm(self, event_id: str, device_id: str) -> dict[str, Any]:
        with self._lock:
            event = self._get(event_id, device_id)
            if event["status"] != "awaiting_confirmation":
                raise MobileAgentError("mobile action is not awaiting confirmation")
            event["status"] = "pending"
            event["updated_at"] = self._clock()
            return self._copy(event)

    def claim_next(self, device_id: str) -> dict[str, Any] | None:
        self._check_device_id(device_id)
        with self._lock:
            now = self._clock()
            for event_id in self._order:
                event = self._events[event_id]
                if (
                    event["device_id"] == device_id
                    and event["status"] == "claimed"
                    and event["claimed_at"] is not None
                    and now - event["claimed_at"] >= CLAIM_LEASE_SECONDS
                ):
                    event["status"] = "pending"
                    event["claimed_at"] = None
                    event["updated_at"] = now
            for event_id in self._order:
                event = self._events[event_id]
                if event["device_id"] == device_id and event["status"] == "pending":
                    event["status"] = "claimed"
                    event["claimed_at"] = now
                    event["updated_at"] = event["claimed_at"]
                    return self._copy(event)
        return None

    def report(
        self,
        event_id: str,
        device_id: str,
        *,
        success: bool,
        output: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            event = self._get(event_id, device_id)
            if event["status"] != "claimed":
                raise MobileAgentError("only a claimed mobile action can be reported")
            safe_output = redact(output[:MAX_RESULT_TEXT])
            safe_code = re.sub(r"[^a-z0-9_-]", "", error_code.lower())[:80]
            event["status"] = "completed" if success else "failed"
            event["result"] = {"success": success, "output": safe_output, "error_code": safe_code}
            event["diagnostic"] = None if success else _diagnose(safe_code, str(safe_output))
            event["updated_at"] = self._clock()
            return self._copy(event)

    def list_events(
        self,
        device_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._check_device_id(device_id)
        if status is not None and status not in _STATUS:
            raise MobileAgentError("status is invalid")
        with self._lock:
            events = [
                self._copy(self._events[event_id])
                for event_id in reversed(self._order)
                if self._events[event_id]["device_id"] == device_id
                and (status is None or self._events[event_id]["status"] == status)
            ]
        return events[:max(1, min(limit, 100))]

    def diagnostics(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "event_id": event["id"],
                "action": event["action"],
                "diagnostic": event["diagnostic"],
                "recorded_at": event["updated_at"],
            }
            for event in self.list_events(device_id, status="failed", limit=limit)
        ]

    def report_location(
        self,
        device_id: str,
        *,
        latitude: float,
        longitude: float,
        accuracy_m: float,
        collected_at: float,
    ) -> dict[str, float]:
        self._check_device_id(device_id)
        now = self._clock()
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise MobileAgentError("location coordinates are invalid")
        if not 0 <= accuracy_m <= 100_000:
            raise MobileAgentError("location accuracy is invalid")
        if collected_at < now - LOCATION_TTL_SECONDS or collected_at > now + 300:
            raise MobileAgentError("location timestamp is invalid")
        location = {
            "latitude": round(float(latitude), 6),
            "longitude": round(float(longitude), 6),
            "accuracy_m": round(float(accuracy_m), 1),
            "collected_at": float(collected_at),
            "received_at": now,
        }
        with self._lock:
            history = self._locations.setdefault(device_id, [])
            if not history or collected_at > history[-1]["collected_at"]:
                history.append(location)
            elif collected_at == history[-1]["collected_at"]:
                history[-1] = location
            else:
                history.append(location)
                history.sort(key=lambda item: item["collected_at"])
            cutoff = now - LOCATION_TTL_SECONDS
            self._locations[device_id] = [item for item in history if item["received_at"] >= cutoff][-1000:]
            return copy.deepcopy(location)

    def latest_location(self, device_id: str) -> dict[str, float] | None:
        self._check_device_id(device_id)
        with self._lock:
            history = self._locations.get(device_id, [])
            if not history:
                return None
            location = history[-1]
            if self._clock() - location["received_at"] > LOCATION_TTL_SECONDS:
                self._locations.pop(device_id, None)
                return None
            return copy.deepcopy(location)

    def location_history(self, device_id: str, *, since: float | None = None, limit: int = 1000) -> list[dict[str, float]]:
        self._check_device_id(device_id)
        cutoff = self._clock() - LOCATION_TTL_SECONDS
        with self._lock:
            points = [
                copy.deepcopy(item)
                for item in self._locations.get(device_id, [])
                if item["received_at"] >= cutoff and (since is None or item["collected_at"] >= since)
            ]
        return list(reversed(points[-max(1, min(limit, 1000)):]))

    def observer_events(
        self,
        device_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return [
            self._observer_copy(event)
            for event in self.list_events(device_id, status=status, limit=limit)
        ]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    action: ActionName
    payload: dict[str, Any] = Field(default_factory=dict)


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DeviceEnrollmentRequest(DeviceRequest):
    role: Literal["operation", "observer"] = "operation"
    observed_device_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )


class ActionReport(DeviceRequest):
    success: bool
    output: str = Field(default="", max_length=MAX_RESULT_TEXT)
    error_code: str = Field(default="", max_length=80)


class LocationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    collected_at: float = Field(gt=0)


coordinator = MobileAgentCoordinator()


def build_router(require_auth: Callable[..., None]) -> APIRouter:
    router = APIRouter(
        prefix="/api/mobile-agent",
        tags=["mobile-agent"],
    )

    def handle_error(exc: MobileAgentError) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc), headers=NO_STORE)

    @router.post("/devices/enroll", dependencies=[Depends(require_auth)], status_code=201)
    def enroll_device(body: DeviceEnrollmentRequest) -> JSONResponse:
        try:
            token = coordinator.enroll(
                body.device_id,
                role=body.role,
                observed_device_id=body.observed_device_id,
            )
        except MobileAgentError as exc:
            raise handle_error(exc) from exc
        return JSONResponse(
            {
                "device_id": body.device_id,
                "device_token": token,
                "role": body.role,
                "observed_device_id": body.observed_device_id,
            },
            status_code=201,
            headers=NO_STORE,
        )

    @router.post("/actions", dependencies=[Depends(require_auth)], status_code=201)
    def submit_action(body: ActionRequest) -> JSONResponse:
        try:
            event = coordinator.submit(body.device_id, body.action, body.payload)
        except MobileAgentError as exc:
            raise handle_error(exc) from exc
        return JSONResponse(event, status_code=201, headers=NO_STORE)

    @router.post("/actions/{event_id}/confirm", dependencies=[Depends(require_auth)])
    def confirm_action(event_id: str, body: DeviceRequest) -> JSONResponse:
        try:
            event = coordinator.confirm(event_id, body.device_id)
        except MobileAgentError as exc:
            raise handle_error(exc) from exc
        return JSONResponse(event, headers=NO_STORE)

    @router.post("/devices/{device_id}/claim")
    def claim_action(
        device_id: str,
        x_device_token: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(device_id, x_device_token or "", role="operation")
            event = coordinator.claim_next(device_id)
        except MobileAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"action": event}, headers=NO_STORE)

    @router.post("/actions/{event_id}/report")
    def report_action(
        event_id: str,
        body: ActionReport,
        x_device_token: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(body.device_id, x_device_token or "", role="operation")
        except MobileAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers=NO_STORE) from exc
        try:
            event = coordinator.report(
                event_id,
                body.device_id,
                success=body.success,
                output=body.output,
                error_code=body.error_code,
            )
        except MobileAgentError as exc:
            raise handle_error(exc) from exc
        return JSONResponse(event, headers=NO_STORE)

    @router.post("/devices/{device_id}/location")
    def report_location(
        device_id: str,
        body: LocationReport,
        x_device_token: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(device_id, x_device_token or "", role="operation")
            location = coordinator.report_location(device_id, **body.model_dump())
            if location_history.configured():
                location = location_history.insert(device_id, location)
        except MobileAgentError as exc:
            raise HTTPException(status_code=401 if "authentication" in str(exc) else 400, detail=str(exc), headers=NO_STORE) from exc
        except LocationHistoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"location": location}, headers=NO_STORE)

    @router.get("/devices/{device_id}/actions", dependencies=[Depends(require_auth)])
    def list_actions(
        device_id: str,
        status: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        try:
            events = coordinator.list_events(device_id, status=status, limit=limit)
        except MobileAgentError as exc:
            raise handle_error(exc) from exc
        return JSONResponse({"actions": events}, headers=NO_STORE)

    @router.get("/devices/{device_id}/diagnostics", dependencies=[Depends(require_auth)])
    def list_diagnostics(
        device_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        return JSONResponse(
            {"diagnostics": coordinator.diagnostics(device_id, limit=limit)},
            headers=NO_STORE,
        )

    @router.get("/devices/{device_id}/location", dependencies=[Depends(require_auth)])
    def get_location(device_id: str) -> JSONResponse:
        try:
            location = location_history.latest(device_id) if location_history.configured() else coordinator.latest_location(device_id)
        except MobileAgentError as exc:
            raise handle_error(exc) from exc
        except LocationHistoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"location": location}, headers=NO_STORE)

    @router.get("/devices/{device_id}/location/history", dependencies=[Depends(require_auth)])
    def get_location_history(
        device_id: str,
        since: Annotated[float | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> JSONResponse:
        try:
            points = (
                location_history.list(device_id, since=since, limit=limit)
                if location_history.configured()
                else coordinator.location_history(device_id, since=since, limit=limit)
            )
        except (MobileAgentError, LocationHistoryError) as exc:
            status = 503 if isinstance(exc, LocationHistoryError) else 400
            raise HTTPException(status_code=status, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"locations": points, "retention_days": RETENTION_DAYS}, headers=NO_STORE)

    @router.get("/observers/{observer_id}/devices/{device_id}/actions")
    def observer_actions(
        observer_id: str,
        device_id: str,
        x_device_token: Annotated[str | None, Header()] = None,
        status: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(
                observer_id,
                x_device_token or "",
                role="observer",
                observed_device_id=device_id,
            )
            events = coordinator.observer_events(device_id, status=status, limit=limit)
        except MobileAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"actions": events}, headers=NO_STORE)

    @router.get("/observers/{observer_id}/devices/{device_id}/diagnostics")
    def observer_diagnostics(
        observer_id: str,
        device_id: str,
        x_device_token: Annotated[str | None, Header()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(
                observer_id,
                x_device_token or "",
                role="observer",
                observed_device_id=device_id,
            )
            diagnostics = coordinator.diagnostics(device_id, limit=limit)
        except MobileAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"diagnostics": diagnostics}, headers=NO_STORE)

    @router.get("/observers/{observer_id}/devices/{device_id}/location")
    def observer_location(
        observer_id: str,
        device_id: str,
        x_device_token: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(
                observer_id,
                x_device_token or "",
                role="observer",
                observed_device_id=device_id,
            )
            location = location_history.latest(device_id) if location_history.configured() else coordinator.latest_location(device_id)
        except MobileAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers=NO_STORE) from exc
        except LocationHistoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"location": location}, headers=NO_STORE)

    @router.get("/observers/{observer_id}/devices/{device_id}/location/history")
    def observer_location_history(
        observer_id: str,
        device_id: str,
        x_device_token: Annotated[str | None, Header()] = None,
        since: Annotated[float | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    ) -> JSONResponse:
        try:
            coordinator.authenticate_device(
                observer_id,
                x_device_token or "",
                role="observer",
                observed_device_id=device_id,
            )
            points = (
                location_history.list(device_id, since=since, limit=limit)
                if location_history.configured()
                else coordinator.location_history(device_id, since=since, limit=limit)
            )
        except MobileAgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers=NO_STORE) from exc
        except LocationHistoryError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers=NO_STORE) from exc
        return JSONResponse({"locations": points, "retention_days": RETENTION_DAYS}, headers=NO_STORE)

    return router
