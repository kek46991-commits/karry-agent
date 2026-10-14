"""karry-agent の軽量 FastAPI REST / chat UI shell。"""
from __future__ import annotations

import threading
import uuid
import json
import os
import re
import logging
import secrets
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from karry_agent.calendar_secrets import calendar_secret_status
from karry_agent.core import AgentCore, AgentMetrics, AgentResult
from karry_agent.memory import ConversationMemory
from karry_agent.privacy import PrivateModeSession, has_passphrase
from karry_agent.tools import add_calendar_event, build_default_registry
from karry_agent.agent_core import dispatch_tool
from karry_agent.self_healing import healing_engine, redact
from karry_agent.repair_pipeline import configure_repair_pipeline
from karry_agent import instant_website
from karry_agent import website_storage

logger = logging.getLogger(__name__)

app = FastAPI(
    title="karry-agent API",
    version="0.2.0",
    description="Gemini Function Calling agent API with chat and execution tracking",
)
STATIC_DIR = Path(__file__).parent / "static"
instant_website.configure_static_root(STATIC_DIR)


@app.get("/static/instant/{filename}", include_in_schema=False)
def instant_artifact(filename: str):
    try:
        artifact = instant_website.public_artifact_content(filename)
    except website_storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Cache-Control": "no-store"}) from None
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    if isinstance(artifact, bytes):
        return Response(artifact, media_type="text/html", headers=instant_website.PUBLIC_HEADERS)
    return FileResponse(artifact, media_type="text/html", headers=instant_website.PUBLIC_HEADERS)


class ArtifactStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        parts = Path(os.path.normpath(path)).parts
        if parts and parts[0] == "instant":
            if len(parts) != 2:
                raise HTTPException(status_code=404, detail="artifact not found")
            if scope.get("method") not in {"GET", "HEAD"}:
                raise HTTPException(status_code=405, detail="method not allowed")
            return await run_in_threadpool(instant_artifact, parts[1])
        response = await super().get_response(path, scope)
        target = getattr(response, "path", None)
        if target is not None and Path(target).resolve().is_relative_to(instant_website.INSTANT_DIR.resolve()):
            return await run_in_threadpool(instant_artifact, Path(target).resolve().name)
        return response


app.mount("/static", ArtifactStaticFiles(directory=STATIC_DIR), name="static")

try:
    _REPAIR_PIPELINE = configure_repair_pipeline(healing_engine, Path(__file__).parent)
except Exception as exc:
    _REPAIR_PIPELINE = None
    healing_engine.analyze_and_heal(exc, {"source": "repair_configuration"})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    healing = healing_engine.analyze_and_heal(exc, {"source": "api", "method": request.method,
                                                  "path": request.url.path, "direction": "inbound"})
    return JSONResponse(status_code=500, content={
        "detail": "Internal Agent Error Handled by Self-Healing Core", "healing_status": healing})


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    content = {"detail": redact(exc.detail)}
    if exc.status_code == 401 or exc.status_code >= 500:
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        content["healing_status"] = healing_engine.analyze_and_heal(cause, {
            "source": "api_http", "method": request.method, "path": request.url.path,
            "direction": "inbound", "status_code": exc.status_code})
        if exc.status_code == 500 and exc.__cause__ is not None:
            content["detail"] = "agent execution failed"
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    healing_engine.analyze_and_heal(ValueError("request validation failed"),
        {"source": "api_validation", "method": request.method, "path": request.url.path})
    errors = [{"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
              for error in exc.errors()[:20]]
    return JSONResponse(status_code=422, content={"detail": errors})


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000, description="エージェントへの指示")
    session_id: str | None = Field(default=None, max_length=100)
    stream: bool = Field(default=False, description="SSE 形式で実行イベントを返す")


class AgentTaskRequest(BaseModel):
    """旧サンプル形式との互換リクエスト。内部では prompt に変換する。"""

    task: str = Field(..., min_length=1, max_length=20_000, description="エージェントへのタスク")
    session_id: str | None = Field(default=None, max_length=100)


class InstantWebsitePayload(BaseModel):
    html_content: str = Field(..., min_length=1, max_length=instant_website.MAX_HTML_BYTES)
    filename: str | None = Field(default=None, max_length=200)


class InstantWebsiteConfirmation(BaseModel):
    approval_id: str = Field(..., min_length=32, max_length=32)
    sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    confirmed: bool


class RunResponse(BaseModel):
    run_id: str
    session_id: str | None = None
    status: str
    text: str
    steps: int
    tool_calls: int
    private_mode: bool = False
    trace: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any]


class AgentRunResponse(BaseModel):
    """`/agent/run` の互換レスポンス。実行の詳細も保持する。"""

    status: str
    task: str
    thought_process: str
    next_action: str
    run_id: str
    session_id: str | None = None
    private_mode: bool = False
    steps: int
    tool_calls: int
    trace: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any]


class SessionResponse(BaseModel):
    session_id: str
    history: list[dict[str, Any]]


_RUNS: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, ConversationMemory] = {}
_PRIVATE_SESSIONS: dict[str, PrivateModeSession] = {}
_PENDING_CALENDAR: dict[str, dict[str, Any]] = {}
_STATE_LOCK = threading.Lock()
_MAX_RUNS = 100
_MAX_SESSIONS = 100


_BEARER = HTTPBearer(auto_error=False)


def require_api_auth(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None) -> None:
    """Preserve existing default API behavior, but require auth when a token or shell is enabled."""
    token = os.getenv("KARRY_API_TOKEN")
    if not token and os.getenv("KARRY_ALLOW_SHELL") == "1":
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is required for shell execution")
    if token and (credentials is None or not secrets.compare_digest(credentials.credentials, token)):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def require_tool_auth(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None) -> None:
    """New direct tool routes always fail closed when no owner token is configured."""
    if not os.getenv("KARRY_API_TOKEN"):
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is not configured")
    require_api_auth(credentials)


class ToolRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


def get_agent() -> AgentCore:
    """依存性注入ポイント。テストや別デプロイ構成で差し替え可能。"""
    try:
        return AgentCore()
    except ValueError as exc:
        # Dependency injection の外側で発生する設定エラーは、FastAPI の
        # 無情報な 500 ではなく、運用者が修正できる 503 として返す。
        healing_engine.analyze_and_heal(exc, {"source": "agent_configuration"})
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _metrics(result: AgentResult) -> dict[str, Any]:
    return asdict(result.metrics)


def _trace(result: AgentResult) -> list[dict[str, Any]]:
    return [asdict(event) for event in result.trace]


def _new_run() -> str:
    run_id = uuid.uuid4().hex
    with _STATE_LOCK:
        if len(_RUNS) >= _MAX_RUNS:
            oldest = next(iter(_RUNS))
            _RUNS.pop(oldest, None)
        _RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trace": [],
        }
    return run_id


def _finish_run(run_id: str, response: RunResponse) -> None:
    with _STATE_LOCK:
        _RUNS[run_id] = response.model_dump()


def _session(session_id: str | None) -> tuple[str | None, ConversationMemory | None]:
    if not session_id:
        return None, None
    with _STATE_LOCK:
        if session_id not in _SESSIONS:
            if len(_SESSIONS) >= _MAX_SESSIONS:
                oldest = next(iter(_SESSIONS))
                _SESSIONS.pop(oldest, None)
            _SESSIONS[session_id] = ConversationMemory(max_history=10)
        return session_id, _SESSIONS[session_id]


def _private_mode(session_id: str | None, prompt: str) -> bool:
    """入力またはセッション認証に基づき、現在の実行だけのモードを判定する。"""
    if not session_id:
        return has_passphrase(prompt)
    with _STATE_LOCK:
        auth = _PRIVATE_SESSIONS.setdefault(session_id, PrivateModeSession())
        return auth.authenticate(prompt)


def _calendar_confirmation(prompt: str) -> str | None:
    """明示的な予定登録確認だけを yes / no として判定する。"""
    text = re.sub(r"[\s　。、，！!？?]+", "", prompt.strip().lower())
    if not text:
        return None
    if any(word in text for word in ("キャンセル", "取消", "取り消し", "やめて", "いいえ", "no")):
        return "cancel"
    if text in {"はい", "yes", "y", "ok", "okay", "了解", "承認", "登録", "実行"}:
        return "confirm"
    if any(phrase in text for phrase in ("登録してください", "登録して", "追加してください", "追加して", "実行してください")):
        return "confirm"
    return None


def _pending_calendar_result(session_id: str | None, prompt: str) -> AgentResult | None:
    """保留中の予定に対する明示確認を、モデル任せにせず確実に実行する。"""
    if not session_id:
        return None
    decision = _calendar_confirmation(prompt)
    if decision is None:
        return None
    with _STATE_LOCK:
        pending = dict(_PENDING_CALENDAR.get(session_id, {}))
    if not pending:
        return None
    if decision == "cancel":
        with _STATE_LOCK:
            _PENDING_CALENDAR.pop(session_id, None)
        return AgentResult("Google Calendarへの登録をキャンセルしました。", 1, 0)

    try:
        result = add_calendar_event(**pending, confirmed=True)
    except Exception as exc:
        # Confirmed insert is deliberately outside the model loop, so an API/
        # auth failure must become a normal result instead of a generic 500.
        healing_engine.analyze_and_heal(exc, {"source": "confirmed_tool", "tool_name": "add_calendar_event"})
        return AgentResult(
            f"Google Calendarへの登録に失敗しました（{type(exc).__name__}）。認証状態を確認して再試行してください。",
            1,
            1,
            metrics=AgentMetrics(tool_calls=1, errors=1),
        )
    if result.get("created"):
        with _STATE_LOCK:
            _PENDING_CALENDAR.pop(session_id, None)
        event_id = result.get("event_id") or "不明"
        return AgentResult(f"Google Calendarに登録しました。イベントID: {event_id}", 1, 1, metrics=AgentMetrics(tool_calls=1))
    healing_engine.observe_result(result, {"source": "confirmed_tool", "tool_name": "add_calendar_event"})
    return AgentResult(f"Google Calendarへの登録に失敗しました: {result.get('error', '不明なエラー')}", 1, 1, metrics=AgentMetrics(tool_calls=1, errors=1))


def _remember_calendar_preview(session_id: str | None, result: AgentResult) -> None:
    """モデルが返したCalendarプレビューを次の確認入力に結び付ける。"""
    if not session_id:
        return
    for event in result.trace:
        if event.action != "add_calendar_event" or not isinstance(event.observation, dict):
            continue
        observation = event.observation
        with _STATE_LOCK:
            if observation.get("confirmation_required"):
                args = event.arguments
                _PENDING_CALENDAR[session_id] = {
                    "summary": args.get("summary", ""),
                    "start_time": args.get("start_time", ""),
                    "duration_minutes": args.get("duration_minutes", 60),
                }
                for key in ("end_time", "description"):
                    if key in args:
                        _PENDING_CALENDAR[session_id][key] = args[key]
                preview = observation.get("event_preview", {})
                if preview.get("start", {}).get("dateTime") and preview.get("end", {}).get("dateTime"):
                    _PENDING_CALENDAR[session_id]["start_time"] = preview["start"]["dateTime"]
                    _PENDING_CALENDAR[session_id]["end_time"] = preview["end"]["dateTime"]
            elif observation.get("created"):
                _PENDING_CALENDAR.pop(session_id, None)
        break


def _run_agent(agent: AgentCore, prompt: str, private_mode: bool, current_prompt: str | None = None) -> AgentResult:
    # 既存の差し替え用 FakeAgent（run(prompt) のみ）との互換性を保つ。
    if isinstance(agent, AgentCore):
        return agent.run(prompt, private_mode=private_mode, current_prompt=current_prompt)
    if private_mode:
        return agent.run(prompt, private_mode=True)
    return agent.run(prompt)


def _execute(request: RunRequest, agent: AgentCore) -> RunResponse:
    run_id = _new_run()
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
    session_id, memory = _session(request.session_id)
    private_mode = _private_mode(session_id, request.prompt)
    prompt = request.prompt
    if memory and memory.get_history():
        context = "\n".join(f"{item['role']}: {item['content']}" for item in memory.get_history()[-6:])
        prompt = f"会話履歴:\n{context}\n\n今回の依頼:\n{request.prompt}"
    try:
        result = _pending_calendar_result(session_id, request.prompt)
        if result is None:
            result = _run_agent(agent, prompt, private_mode, current_prompt=request.prompt)
    except ValueError as exc:
        with _STATE_LOCK:
            _RUNS[run_id]["status"] = "failed"
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        with _STATE_LOCK:
            _RUNS[run_id]["status"] = "failed"
        raise HTTPException(status_code=500, detail="agent execution failed") from exc
    _remember_calendar_preview(session_id, result)
    if memory:
        memory.add_message("user", request.prompt)
        memory.add_message("model", result.text)
    response = RunResponse(
        run_id=run_id,
        session_id=session_id,
        status=getattr(result, "status", "completed"),
        text=result.text,
        steps=result.steps,
        tool_calls=result.tool_calls,
        private_mode=private_mode,
        trace=_trace(result),
        metrics=_metrics(result),
    )
    _finish_run(run_id, response)
    return response


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _stream_chat(request: RunRequest, agent: AgentCore):
    """実行開始後にトレースを SSE として返す最小ストリーミング基盤。"""
    try:
        yield _sse("started", {"prompt": request.prompt})
        response = _execute(request, agent)
        for index, event in enumerate(response.trace, start=1):
            yield _sse("step", {"index": index, **event})
        yield _sse("completed", response.model_dump())
    except HTTPException as exc:
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        healing = healing_engine.analyze_and_heal(cause, {"source": "api_stream", "status_code": exc.status_code})
        yield _sse("error", {"status_code": exc.status_code, "detail": redact(exc.detail), "healing_status": healing})
    except Exception as exc:
        healing = healing_engine.analyze_and_heal(exc, {"source": "api_stream", "operation": "serialize_or_execute"})
        # Never put the failing/cyclic payload or raw exception into the fallback.
        yield _sse("error", {"status_code": 500, "detail": "agent stream failed", "healing_status": healing})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "karry-agent"}


@app.get("/health/calendar")
def calendar_health() -> dict[str, Any]:
    """Calendar 用 Secret の有無と形式だけを返す。値は返さない。"""
    status = calendar_secret_status(dict(os.environ))
    return {"ready": all(entry["valid"] for entry in status.values()), "env": status}


@app.get("/", include_in_schema=False)
def index():
    from fastapi.responses import FileResponse

    return FileResponse(STATIC_DIR / "index.html")


@app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_auth)])
def run_agent(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse:
    return _execute(request, agent)


@app.post("/agent/run", response_model=AgentRunResponse, dependencies=[Depends(require_api_auth)])
def run_agent_compat(
    request: AgentTaskRequest,
) -> AgentRunResponse:
    """旧 Jimmy-Core サンプルの task 形式を既存の安全な AgentCore へ接続する。"""
    # Body の min_length 検証を先に完了させるため、この互換ルートだけは
    # AgentCore の依存性を関数内で解決する。テストや別デプロイからの
    # dependency override は従来どおり尊重する。
    agent_factory = app.dependency_overrides.get(get_agent, get_agent)
    agent = agent_factory()
    response = _execute(
        RunRequest(prompt=request.task, session_id=request.session_id),
        agent,
    )
    return AgentRunResponse(
        status=response.status,
        task=request.task,
        thought_process=response.text,
        next_action="実行完了または次のステップへ移行",
        run_id=response.run_id,
        session_id=response.session_id,
        private_mode=response.private_mode,
        steps=response.steps,
        tool_calls=response.tool_calls,
        trace=response.trace,
        metrics=response.metrics,
    )


@app.post("/chat", response_model=None, dependencies=[Depends(require_api_auth)])
def chat(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse | StreamingResponse:
    """会話履歴を保持しながらエージェントを呼び出す。"""
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
    if request.stream:
        return StreamingResponse(_stream_chat(request, agent), media_type="text/event-stream")
    return _execute(request, agent)


@app.get("/sessions/{session_id}", response_model=SessionResponse, dependencies=[Depends(require_api_auth)])
def get_session(session_id: str) -> SessionResponse:
    with _STATE_LOCK:
        memory = _SESSIONS.get(session_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(session_id=session_id, history=memory.get_history())


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_auth)])
def get_run(run_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/api/agent/tools", dependencies=[Depends(require_tool_auth)])
def agent_tools() -> dict[str, Any]:
    registry = build_default_registry()
    return {"tools": [tool.declaration for tool in registry.tools.values()]}


@app.post("/api/agent/tools/execute", dependencies=[Depends(require_tool_auth)])
def execute_agent_tool(request: ToolRequest) -> dict[str, Any]:
    # Direct authenticated owner calls supply the exact final HTML themselves.
    # Model dispatch instead calls the staged registry wrapper and cannot approve.
    if request.tool_name == "deploy_instant_website":
        if set(request.arguments) - {"html_content", "filename"}:
            raise HTTPException(status_code=400, detail="invalid publication arguments")
        try:
            payload = InstantWebsitePayload(**request.arguments)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid HTML publication payload") from None
        return {"tool_name": request.tool_name,
                "result": instant_website.deploy_instant_website(payload.html_content, payload.filename)}
    return {"tool_name": request.tool_name, "result": dispatch_tool(request.tool_name, request.arguments)}


@app.post("/api/agent/websites/confirm", dependencies=[Depends(require_tool_auth)])
def confirm_website(request: InstantWebsiteConfirmation) -> dict[str, Any]:
    return instant_website.confirm_instant_website(request.approval_id, request.sha256, request.confirmed)


@app.get("/api/agent/websites/storage", dependencies=[Depends(require_tool_auth)])
def website_storage_status(verify: bool = False) -> dict[str, Any]:
    """Owner-only diagnostics. No secrets, no objects are created by this check."""
    try:
        mode = website_storage.storage_mode()
        if mode == "local":
            return {"mode": mode, "configuration_valid": True, "durable": False, "connection_status": "not_applicable"}
        config = website_storage.load_storage_config()
        if verify:
            try:
                website_storage.S3WebsiteStore(config).client.list_objects_v2(Bucket=config.bucket, Prefix=config.prefix, MaxKeys=1)
            except Exception:
                return {"mode": mode, "configuration_valid": True, "durable": True, "connection_status": "failed"}
        return {"mode": mode, "configuration_valid": True, "durable": True, "connection_status": "verified" if verify else "not_checked"}
    except website_storage.StorageError as exc:
        return {"mode": os.getenv("KARRY_WEBSITE_STORAGE", "s3"), "configuration_valid": False, "durable": False,
                "connection_status": "not_checked", "error_code": exc.code, "error": str(exc)}


@app.get("/api/agent/healing", dependencies=[Depends(require_tool_auth)])
def healing_status() -> dict[str, Any]:
    return healing_engine.status()


@app.get("/api/agent/healing/incidents", dependencies=[Depends(require_tool_auth)])
def healing_incidents() -> dict[str, Any]:
    return {"incidents": healing_engine.error_logs}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
