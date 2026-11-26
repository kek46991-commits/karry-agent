"""Jimmy Core API: memory, state, think, dispatch, webhook callback."""
from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .config import KILLSWITCH_AGENT, Settings, load_settings
from .dispatch import TERMINAL, callback_token, send
from .store import next_id, open_store, snapshot, utcnow
from .thinking import think

MAX_PAYLOAD_BYTES = 64_000
_bearer = HTTPBearer(auto_error=False)


def _small_json(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"must be at most {MAX_PAYLOAD_BYTES} bytes as JSON")
    return value


class TaskUpdate(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    title: str | None = Field(default=None, max_length=300)
    status: Literal["todo", "in_progress", "blocked", "done", "cancelled"] = "todo"


class MemoryIn(BaseModel):
    kind: Literal["conversation", "decision", "task", "note", "event"] = "conversation"
    content: str = Field(min_length=1, max_length=20_000)
    role: Literal["user", "jimmy", "agent", "system"] = "user"
    task: TaskUpdate | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _meta = field_validator("metadata")(_small_json)


class ThinkIn(BaseModel):
    situation: str = Field(min_length=1, max_length=8_000)
    goal: str | None = Field(default=None, max_length=2_000)
    context: dict[str, Any] = Field(default_factory=dict)
    use_llm: bool = True
    remember: bool = True

    _ctx = field_validator("context")(_small_json)


class DispatchIn(BaseModel):
    agent: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    action: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)

    _payload = field_validator("payload")(_small_json)


class CallbackIn(BaseModel):
    job_id: str = Field(min_length=1, max_length=40)
    status: Literal["progress", "completed", "failed", "cancelled"]
    result: Any = None
    error: str | None = Field(default=None, max_length=2_000)


class KillswitchIn(BaseModel):
    engaged: bool
    reason: str = Field(default="", max_length=500)


def create_app(settings: Settings | None = None, transport: Any = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or load_settings()
        app.state.store = open_store(app.state.settings.database_url, app.state.settings.data_dir)
        app.state.transport = transport
        yield

    app = FastAPI(title="Jimmy Core", version=__version__, lifespan=lifespan,
                  description="記憶・人格・思考・指示出しだけに特化した中枢 API")

    def cfg(request: Request) -> Settings:
        return request.app.state.settings

    def require_token(request: Request,
                      credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> None:
        expected = cfg(request).api_token
        if not expected:
            raise HTTPException(503, "JIMMY_CORE_API_TOKEN is not configured")
        if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
            raise HTTPException(401, "invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_token)]

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"service": "jimmy-core", "version": __version__,
                "endpoints": ["POST /memory", "GET /memory", "GET /state", "POST /think",
                              "POST /dispatch", "GET /dispatch/{job_id}", "POST /webhook/callback",
                              "POST /killswitch", "GET /agents"]}

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        s = cfg(request)
        return {"status": "ok", "service": "jimmy-core", "version": __version__,
                "storage": request.app.state.store.backend, "auth_configured": bool(s.api_token),
                "agents": sorted(s.agents), "llm": bool(s.gemini_api_key)}

    @app.post("/memory", status_code=201, dependencies=auth)
    def add_memory(body: MemoryIn, request: Request) -> dict[str, Any]:
        with request.app.state.store.transaction() as doc:
            entry = {"id": next_id(doc, "mem"), "kind": body.kind, "role": body.role, "content": body.content,
                     "metadata": body.metadata, "created_at": utcnow()}
            if body.task:
                task = doc["state"]["tasks"].get(body.task.id, {"id": body.task.id, "created_at": entry["created_at"]})
                task.update(status=body.task.status, updated_at=entry["created_at"])
                if body.task.title:
                    task["title"] = body.task.title
                doc["state"]["tasks"][body.task.id] = task
                entry["task_id"] = body.task.id
            doc["memory"].append(entry)
            return snapshot(entry)

    @app.get("/memory", dependencies=auth)
    def list_memory(request: Request, kind: str | None = None,
                    limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, Any]:
        items = request.app.state.store.read()["memory"]
        if kind:
            items = [item for item in items if item["kind"] == kind]
        return {"items": items[-limit:], "count": len(items)}

    @app.get("/state", dependencies=auth)
    def get_state(request: Request) -> dict[str, Any]:
        doc = request.app.state.store.read()
        memory, tasks = doc["memory"], list(doc["state"]["tasks"].values())
        jobs = sorted(doc["jobs"].values(), key=lambda job: job["created_at"])
        return {
            "user": doc["state"]["user"],
            "killswitch": doc["state"].get("killswitch", {"engaged": False}),
            "current_tasks": [t for t in tasks if t.get("status") not in {"done", "cancelled"}],
            "finished_tasks": [t for t in tasks if t.get("status") in {"done", "cancelled"}][-20:],
            "recent_decisions": [m for m in memory if m["kind"] == "decision"][-10:],
            "recent_conversation": [m for m in memory if m["kind"] == "conversation"][-20:],
            "last_thought": next((m for m in reversed(memory) if m["kind"] == "thought"), None),
            "active_jobs": [j for j in jobs if j["status"] not in TERMINAL],
            "recent_jobs": jobs[-10:],
            "agents": [agent.public() for agent in cfg(request).agents.values()],
            "counts": {"memory": len(memory), "tasks": len(tasks), "jobs": len(jobs)},
            "updated_at": doc.get("updated_at"),
        }

    @app.post("/think", dependencies=auth)
    async def think_endpoint(body: ThinkIn, request: Request) -> dict[str, Any]:
        store = request.app.state.store
        result = await think(cfg(request), store.read(), body.situation, body.goal, body.context,
                             body.use_llm, request.app.state.transport)
        if body.remember:
            with store.transaction() as doc:
                entry = {"id": next_id(doc, "mem"), "kind": "thought", "role": "jimmy",
                         "content": body.situation[:2_000], "created_at": utcnow(),
                         "metadata": {"risk": result["risk"]["level"], "decision": result["decision"]}}
                doc["memory"].append(entry)
                result["memory_id"] = entry["id"]
        return result

    @app.get("/agents", dependencies=auth)
    def agents(request: Request) -> dict[str, Any]:
        return {"agents": [agent.public() for agent in cfg(request).agents.values()]}

    @app.post("/killswitch", dependencies=auth)
    def killswitch(body: KillswitchIn, request: Request) -> dict[str, Any]:
        with request.app.state.store.transaction() as doc:
            doc["state"]["killswitch"] = {"engaged": body.engaged, "reason": body.reason, "updated_at": utcnow()}
            doc["memory"].append({"id": next_id(doc, "mem"), "kind": "event", "role": "system",
                                  "content": f"killswitch {'engaged' if body.engaged else 'released'}: {body.reason}",
                                  "metadata": {}, "created_at": utcnow()})
            return snapshot(doc["state"]["killswitch"])

    @app.post("/dispatch", status_code=202, dependencies=auth)
    def dispatch(body: DispatchIn, request: Request, background: BackgroundTasks) -> dict[str, Any]:
        s, store = cfg(request), request.app.state.store
        agent = s.agents.get(body.agent)
        if agent is None:
            raise HTTPException(404, f"agent '{body.agent}' is not registered (set JIMMY_AGENT_{body.agent.upper()}_URL)")
        with store.transaction() as doc:
            if doc["state"].get("killswitch", {}).get("engaged") and body.agent != KILLSWITCH_AGENT:
                raise HTTPException(423, "killswitch is engaged; only the killswitch agent can be dispatched")
            now = utcnow()
            job = {"id": next_id(doc, "job"), "agent": body.agent, "action": body.action, "status": "queued",
                   "created_at": now, "updated_at": now, "result": None, "error": None, "events": []}
            doc["jobs"][job["id"]] = job
            job = snapshot(job)
        background.add_task(send, s, store, agent, job, body.payload, str(request.base_url),
                            request.app.state.transport)
        return {"job_id": job["id"], "status": job["status"], "agent": body.agent}

    @app.get("/dispatch/{job_id}", dependencies=auth)
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        job = request.app.state.store.read()["jobs"].get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.post("/webhook/callback")
    def webhook_callback(body: CallbackIn, request: Request,
                         x_jimmy_callback_token: Annotated[str | None, Header()] = None,
                         credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
                         ) -> dict[str, Any]:
        s = cfg(request)
        if not s.callback_secret:
            raise HTTPException(503, "JIMMY_CORE_API_TOKEN is not configured")
        given = x_jimmy_callback_token or (credentials.credentials if credentials else "")
        if not given or not (secrets.compare_digest(given, callback_token(s, body.job_id))
                             or secrets.compare_digest(given, s.api_token)):
            raise HTTPException(401, "invalid callback token")
        with request.app.state.store.transaction() as doc:
            job = doc["jobs"].get(body.job_id)
            if job is None:
                raise HTTPException(404, "job not found")
            if job["status"] in TERMINAL:
                if job["status"] == body.status:
                    return {"job_id": job["id"], "status": job["status"], "duplicate": True}
                raise HTTPException(409, f"job already {job['status']}")
            now = utcnow()
            job["events"] = (job.get("events") or [])[-19:] + [{"status": body.status, "at": now}]
            job.update(status=body.status if body.status != "progress" else "running", updated_at=now)
            if body.result is not None:
                job["result"] = body.result
            if body.error:
                job["error"] = body.error
            if body.status in TERMINAL:
                doc["memory"].append({"id": next_id(doc, "mem"), "kind": "event", "role": "agent",
                                      "content": f"{job['agent']} job {job['id']} {body.status}",
                                      "metadata": {"job_id": job["id"]}, "created_at": now})
            return {"job_id": job["id"], "status": job["status"]}

    return app


app = create_app()
