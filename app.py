"""karry-agent の軽量 FastAPI REST / chat UI shell。"""
from __future__ import annotations

import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from karry_agent.core import AgentCore, AgentResult
from karry_agent.memory import ConversationMemory

app = FastAPI(
    title="karry-agent API",
    version="0.2.0",
    description="Gemini Function Calling agent API with chat and execution tracking",
)


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000, description="エージェントへの指示")
    session_id: str | None = Field(default=None, max_length=100)


class RunResponse(BaseModel):
    run_id: str
    session_id: str | None = None
    status: str
    text: str
    steps: int
    tool_calls: int
    trace: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any]


class SessionResponse(BaseModel):
    session_id: str
    history: list[dict[str, Any]]


_RUNS: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, ConversationMemory] = {}
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


def _execute(request: RunRequest, agent: AgentCore) -> RunResponse:
    run_id = _new_run()
    session_id, memory = _session(request.session_id)
    prompt = request.prompt
    if memory and memory.get_history():
        context = "\n".join(f"{item['role']}: {item['content']}" for item in memory.get_history()[-6:])
        prompt = f"会話履歴:\n{context}\n\n今回の依頼:\n{request.prompt}"
    try:
        result = agent.run(prompt)
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
        trace=_trace(result),
        metrics=_metrics(result),
    )
    _finish_run(run_id, response)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "karry-agent"}


@app.post("/run", response_model=RunResponse)
def run_agent(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse:
    return _execute(request, agent)


@app.post("/chat", response_model=RunResponse)
def chat(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse:
    """会話履歴を保持しながらエージェントを呼び出す。"""
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
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
