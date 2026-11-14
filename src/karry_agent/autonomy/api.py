"""``/api/autonomy``: owner-started autonomous runs, plus the endpoints a separate monitor device uses."""

import hashlib
import os
import secrets
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .. import user_login
from ..auth_token import configured_token, normalize_token, owner_credential_ok
from .devices import MAX_FRAME_BYTES, Device, DeviceError, DeviceRegistry
from .planner import PlanError
from .runner import FINISHED, AutonomousRunner
from .state import RunStateError

NO_STORE = {"Cache-Control": "no-store"}
RunId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
DeviceId = Annotated[str, Path(pattern=r"^[a-f0-9]{16}$")]
DEVICE_COOKIE = "karry_device"
DEVICE_HEADER = "X-Karry-Device"
_BEARER = HTTPBearer(auto_error=False)


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    budget_tokens: int | None = Field(default=None, ge=200, le=8000)
    dry_run: bool = False


class SignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "stop", "resume"]
    reason: str = Field(default="", max_length=500)


class EnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["worker", "monitor"]
    name: str = Field(default="", max_length=40)
    password: str = Field(min_length=1, max_length=200)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    visible: bool | None = None
    sharing: bool | None = None
    battery: float | None = None
    charging: bool | None = None
    app: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=120)
    platform: str | None = Field(default=None, max_length=120)
    run_id: str | None = Field(default=None, max_length=40)


def _monitor_token_ok(supplied: str | None) -> bool:
    monitor = normalize_token(os.getenv("KARRY_MONITOR_TOKEN"))
    supplied = normalize_token(supplied)
    return bool(monitor and supplied) and secrets.compare_digest(supplied.encode(), monitor.encode())


def _enroll_password_ok(supplied: str) -> bool:
    expected = os.getenv("KARRY_DEVICE_ENROLL_PASSWORD", "")
    if not expected:
        raise HTTPException(status_code=503, detail="KARRY_DEVICE_ENROLL_PASSWORD is not configured")
    return secrets.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def _error(exc: RunStateError) -> HTTPException:
    status = {"not_found": 404, "already_running": 409, "finished": 409, "storage_capacity": 409,
              "invalid_run": 400, "invalid_signal": 400}.get(exc.code, 503)
    return HTTPException(status_code=status, detail=str(exc))


def build_router(owner_auth: Callable[..., None], runner: Callable[[], AutonomousRunner],
                 devices: Callable[[], DeviceRegistry]) -> APIRouter:
    router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])
    owner = [Depends(owner_auth)]

    def current_device(request: Request, x_device_token: Annotated[str | None, Header()] = None) -> Device | None:
        token = x_device_token or request.cookies.get(DEVICE_COOKIE)
        device = devices().authenticate(token)
        # Cookie-authenticated writes need a custom header, which a cross-site form cannot send.
        if device and not x_device_token and request.method not in {"GET", "HEAD"} and request.headers.get(DEVICE_HEADER) != "1":
            raise HTTPException(status_code=403, detail=f"{DEVICE_HEADER} header is required")
        return device

    def require_device(device: Annotated[Device | None, Depends(current_device)]) -> Device:
        if device is None:
            raise HTTPException(status_code=401, detail="端末が登録されていません。")
        return device

    def require_worker(device: Annotated[Device, Depends(require_device)]) -> Device:
        if device.role != "worker":
            raise HTTPException(status_code=403, detail="作業端末専用です。")
        return device

    def monitor_or_owner(
        device: Annotated[Device | None, Depends(current_device)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None,
        x_monitor_token: Annotated[str | None, Header()] = None,
    ) -> str:
        """Monitor devices (and the KARRY_MONITOR_TOKEN) read runs/audits and send signals; they cannot start runs."""
        if device is not None and device.role == "monitor":
            return f"monitor:{device.name}"
        if _monitor_token_ok(x_monitor_token):
            return "monitor"
        owner_token = configured_token()
        if not owner_token:
            raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is not configured")
        if credentials is not None and owner_credential_ok(credentials.credentials, owner_token):
            return "owner"
        raise HTTPException(status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"})

    def monitor_device_or_owner(
        device: Annotated[Device | None, Depends(current_device)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None,
    ) -> str:
        if device is not None and device.role == "monitor":
            return f"monitor:{device.name}"
        owner_token = configured_token()
        if owner_token and credentials is not None and owner_credential_ok(credentials.credentials, owner_token):
            return "owner"
        raise HTTPException(status_code=401, detail="見守り端末として登録してください。")

    def device_error(exc: DeviceError) -> HTTPException:
        status = {"not_found": 404, "forbidden": 403, "capacity": 409, "not_sharing": 409, "too_large": 413}.get(exc.code, 400)
        return HTTPException(status_code=status, detail=str(exc))

    @router.post("/devices/enroll")
    def enroll(body: EnrollRequest, request: Request) -> JSONResponse:
        key = f"enroll-ip:{request.client.host if request.client else '-'}"
        if user_login.throttled(key):
            raise HTTPException(status_code=429, detail="失敗が続いたため、しばらく待ってからやり直してください。")
        if not _enroll_password_ok(body.password):
            user_login.record_failure(key)
            raise HTTPException(status_code=401, detail="パスワードが違います。")
        user_login.clear_failures(key)
        try:
            device, token = devices().enroll(body.role, body.name)
        except DeviceError as exc:
            raise device_error(exc) from exc
        response = JSONResponse({"device": device.public()}, headers=NO_STORE)
        response.set_cookie(DEVICE_COOKIE, token, max_age=400 * 24 * 3600, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/api/autonomy")
        return response

    @router.get("/device/me")
    def device_me(device: Annotated[Device, Depends(require_device)]) -> JSONResponse:
        return JSONResponse({"device": device.public()}, headers=NO_STORE)

    @router.post("/device/logout")
    def device_logout(device: Annotated[Device, Depends(require_device)]) -> JSONResponse:
        devices().revoke(device.id, device)
        response = JSONResponse({"ok": True}, headers=NO_STORE)
        response.delete_cookie(DEVICE_COOKIE, path="/api/autonomy")
        return response

    @router.post("/device/heartbeat")
    def heartbeat(body: HeartbeatRequest, device: Annotated[Device, Depends(require_device)]) -> JSONResponse:
        devices().heartbeat(device, body.model_dump(exclude_none=True))
        runs = [s.summary() for s in runner().store.list()][:10] if device.role == "worker" else []
        return JSONResponse({"device": device.public(), "runs": runs}, headers=NO_STORE)

    @router.put("/device/frame")
    async def put_frame(request: Request, device: Annotated[Device, Depends(require_worker)]) -> JSONResponse:
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_FRAME_BYTES:
                raise HTTPException(status_code=413, detail="画像が大きすぎます。")
        try:
            devices().put_frame(device, data)
        except DeviceError as exc:
            raise device_error(exc) from exc
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @router.get("/overview")
    def overview(actor: Annotated[str, Depends(monitor_device_or_owner)]) -> JSONResponse:
        current = runner()
        runs = sorted(current.store.list(), key=lambda s: s.updated_at, reverse=True)[:20]
        return JSONResponse({"viewer": actor, "devices": devices().list(), "events": list(devices().events)[-50:][::-1],
                             "runs": [{**s.to_dict(), "active": current.is_active(s.id)} for s in runs],
                             "monitor": current.gate.describe()}, headers=NO_STORE)

    @router.get("/devices/{device_id}/frame")
    def get_frame(device_id: DeviceId, actor: Annotated[str, Depends(monitor_device_or_owner)]) -> Response:
        frame = devices().frame(device_id)
        if frame is None:
            raise HTTPException(status_code=404, detail="画面は共有されていません。")
        data, mime, _ = frame
        return Response(data, media_type=mime, headers={**NO_STORE, "X-Content-Type-Options": "nosniff"})

    @router.post("/devices/{device_id}/revoke")
    def revoke_device(device_id: DeviceId, actor: Annotated[str, Depends(monitor_device_or_owner)],
                      device: Annotated[Device | None, Depends(current_device)]) -> JSONResponse:
        try:
            devices().revoke(device_id, device)
        except DeviceError as exc:
            raise device_error(exc) from exc
        return JSONResponse({"ok": True}, headers=NO_STORE)

    def view(state: Any) -> dict[str, Any]:
        return {**state.to_dict(), "active": runner().is_active(state.id)}

    @router.post("/runs", dependencies=owner, status_code=202)
    def create_run(body: RunCreate) -> JSONResponse:
        current = runner()
        try:
            if body.dry_run:
                goal, subtasks = current.plan(body.instruction, body.budget_tokens)
                return JSONResponse({"goal": goal, "subtasks": [t.__dict__ for t in subtasks]}, status_code=200, headers=NO_STORE)
            state = current.create(body.instruction, session_id=body.session_id, budget_tokens=body.budget_tokens)
        except PlanError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RunStateError as exc:
            raise _error(exc) from exc
        current.start_background(state.id)
        return JSONResponse(view(state), status_code=202, headers=NO_STORE)

    @router.get("/runs", dependencies=owner)
    def list_runs() -> JSONResponse:
        return JSONResponse({"runs": [s.summary() for s in runner().store.list()]}, headers=NO_STORE)

    @router.get("/runs/{run_id}", dependencies=owner)
    def get_run(run_id: RunId) -> JSONResponse:
        try:
            return JSONResponse(view(runner().store.load(run_id)), headers=NO_STORE)
        except RunStateError as exc:
            raise _error(exc) from exc

    @router.post("/runs/{run_id}/resume", dependencies=owner, status_code=202)
    def resume_run(run_id: RunId) -> JSONResponse:
        current = runner()
        try:
            state = current.store.load(run_id)
        except RunStateError as exc:
            raise _error(exc) from exc
        if state.status in FINISHED:
            raise HTTPException(status_code=409, detail="この実行は終了しています。")
        if current.is_active(run_id):
            raise HTTPException(status_code=409, detail="この実行はすでに進行中です。")
        if state.control == "pause":
            state = current.signal(run_id, "resume", "所有者が再開しました。", "owner")
        if not current.start_background(run_id):
            raise HTTPException(status_code=409, detail="この実行はすでに進行中です。")
        return JSONResponse(view(state), status_code=202, headers=NO_STORE)

    @router.post("/runs/{run_id}/signal")
    def signal_run(run_id: RunId, body: SignalRequest, actor: Annotated[str, Depends(monitor_or_owner)]) -> JSONResponse:
        current = runner()
        try:
            state = current.signal(run_id, body.action, body.reason, actor)
        except RunStateError as exc:
            raise _error(exc) from exc
        if body.action == "resume" and state.status in {"paused", "running"}:
            current.start_background(run_id)
        return JSONResponse(view(state), headers=NO_STORE)

    @router.get("/runs/{run_id}/audit")
    def run_audit(run_id: RunId, actor: Annotated[str, Depends(monitor_or_owner)],
                  after: Annotated[int, Query(ge=0)] = 0) -> JSONResponse:
        current = runner()
        try:
            current.store.load(run_id)
        except RunStateError as exc:
            raise _error(exc) from exc
        log = current.audit(run_id)
        return JSONResponse({"run_id": run_id, "verified": log.verify(), "entries": log.entries(after)}, headers=NO_STORE)

    @router.get("/monitor", dependencies=owner)
    def monitor_config() -> JSONResponse:
        return JSONResponse(runner().gate.describe(), headers=NO_STORE)

    return router
