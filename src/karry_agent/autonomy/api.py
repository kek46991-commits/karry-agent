"""``/api/autonomy``: owner-started autonomous runs, plus the endpoints a separate monitor device uses."""
from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from ..auth_token import configured_token, normalize_token, owner_credential_ok
from .planner import PlanError
from .runner import FINISHED, AutonomousRunner
from .state import RunStateError

NO_STORE = {"Cache-Control": "no-store"}
RunId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
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


def monitor_or_owner(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None,
    x_monitor_token: Annotated[str | None, Header()] = None,
) -> str:
    """The monitor device gets its own ``KARRY_MONITOR_TOKEN``: it can read audits and send signals, nothing else."""
    monitor = normalize_token(os.getenv("KARRY_MONITOR_TOKEN"))
    supplied = normalize_token(x_monitor_token)
    if monitor and supplied and secrets.compare_digest(supplied.encode(), monitor.encode()):
        return "monitor"
    owner = configured_token()
    if not owner:
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is not configured")
    if credentials is not None and owner_credential_ok(credentials.credentials, owner):
        return "owner"
    raise HTTPException(status_code=401, detail="Invalid token", headers={"WWW-Authenticate": "Bearer"})


def _error(exc: RunStateError) -> HTTPException:
    status = {"not_found": 404, "already_running": 409, "finished": 409, "storage_capacity": 409,
              "invalid_run": 400, "invalid_signal": 400}.get(exc.code, 503)
    return HTTPException(status_code=status, detail=str(exc))


def build_router(owner_auth: Callable[..., None], runner: Callable[[], AutonomousRunner]) -> APIRouter:
    router = APIRouter(prefix="/api/autonomy", tags=["autonomy"])
    owner = [Depends(owner_auth)]

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
        if body.action == "resume" and state.status == "paused":
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
