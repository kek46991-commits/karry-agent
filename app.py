"""karry-agent の軽量 FastAPI REST シェル。"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from karry_agent.core import AgentCore, AgentResult

app = FastAPI(
    title="karry-agent API",
    version="0.1.0",
    description="Gemini Function Calling agent API shell",
)


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="エージェントへの指示")


class RunResponse(BaseModel):
    text: str
    steps: int
    tool_calls: int
    metrics: dict[str, Any]


def get_agent() -> AgentCore:
    """依存性注入ポイント。テストや別デプロイ構成で差し替え可能。"""
    return AgentCore()


def _metrics(result: AgentResult) -> dict[str, Any]:
    return {
        "api_calls": result.metrics.api_calls,
        "retries": result.metrics.retries,
        "input_tokens": result.metrics.input_tokens,
        "output_tokens": result.metrics.output_tokens,
        "total_tokens": result.metrics.total_tokens,
        "tool_calls": result.metrics.tool_calls,
        "errors": result.metrics.errors,
        "elapsed_ms": result.metrics.elapsed_ms,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "karry-agent"}


@app.post("/run", response_model=RunResponse)
def run_agent(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse:
    try:
        result = agent.run(request.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="agent execution failed") from exc
    return RunResponse(
        text=result.text,
        steps=result.steps,
        tool_calls=result.tool_calls,
        metrics=_metrics(result),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
