"""karry-agent の軽量 FastAPI REST / chat UI shell。"""
from __future__ import annotations

import threading
import uuid
import json
from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from karry_agent.core import AgentCore, AgentResult
from karry_agent.memory import ConversationMemory
from karry_agent.privacy import PrivateModeSession, has_passphrase

app = FastAPI(
    title="karry-agent API",
    version="0.2.0",
    description="Gemini Function Calling agent API with chat and execution tracking",
)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000, description="エージェントへの指示")
    session_id: str | None = Field(default=None, max_length=100)
    stream: bool = Field(default=False, description="SSE 形式で実行イベントを返す")


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


class SessionResponse(BaseModel):
    session_id: str
    history: list[dict[str, Any]]


_RUNS: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, ConversationMemory] = {}
_PRIVATE_SESSIONS: dict[str, PrivateModeSession] = {}
_STATE_LOCK = threading.Lock()
_MAX_RUNS = 100
_MAX_SESSIONS = 100


def get_agent() -> AgentCore:
    """依存性注入ポイント。テストや別デプロイ構成で差し替え可能。"""
    return AgentCore()


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


def _run_agent(agent: AgentCore, prompt: str, private_mode: bool) -> AgentResult:
    # 既存の差し替え用 FakeAgent（run(prompt) のみ）との互換性を保つ。
    if private_mode:
        return agent.run(prompt, private_mode=True)
    return agent.run(prompt)


def _execute(request: RunRequest, agent: AgentCore) -> RunResponse:
    run_id = _new_run()
    session_id, memory = _session(request.session_id)
    private_mode = _private_mode(session_id, request.prompt)
    prompt = request.prompt
    if memory and memory.get_history():
        context = "\n".join(f"{item['role']}: {item['content']}" for item in memory.get_history()[-6:])
        prompt = f"会話履歴:\n{context}\n\n今回の依頼:\n{request.prompt}"
    try:
        result = _run_agent(agent, prompt, private_mode)
    except ValueError as exc:
        with _STATE_LOCK:
            _RUNS[run_id]["status"] = "failed"
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        with _STATE_LOCK:
            _RUNS[run_id]["status"] = "failed"
        raise HTTPException(status_code=500, detail="agent execution failed") from exc
    if memory:
        memory.add_message("user", request.prompt)
        memory.add_message("model", result.text)
    response = RunResponse(
        run_id=run_id,
        session_id=session_id,
        status="completed",
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
    yield _sse("started", {"prompt": request.prompt})
    try:
        response = _execute(request, agent)
    except HTTPException as exc:
        yield _sse("error", {"status_code": exc.status_code, "detail": exc.detail})
        return
    for index, event in enumerate(response.trace, start=1):
        yield _sse("step", {"index": index, **event})
    yield _sse("completed", response.model_dump())


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "karry-agent"}


@app.get("/", include_in_schema=False)
def index():
    from fastapi.responses import FileResponse

    return FileResponse(STATIC_DIR / "index.html")


@app.post("/run", response_model=RunResponse)
def run_agent(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse:
    return _execute(request, agent)


@app.post("/chat", response_model=None)
def chat(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse | StreamingResponse:
    """会話履歴を保持しながらエージェントを呼び出す。"""
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
    if request.stream:
        return StreamingResponse(_stream_chat(request, agent), media_type="text/event-stream")
    return _execute(request, agent)


@app.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(session_id: str) -> SessionResponse:
    with _STATE_LOCK:
        memory = _SESSIONS.get(session_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(session_id=session_id, history=memory.get_history())


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
