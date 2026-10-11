"""Gemini Function Calling による自律エージェントのコア実装。"""
from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from .tools import ToolRegistry, build_default_registry

load_dotenv()

SYSTEM_INSTRUCTION = """あなたは karry-agent の自律実行コアです。
ユーザーの依頼を達成するため、必要なら利用可能なツールを選択して実行してください。
各ツールの結果（Observation）を確認してから、次の Action または最終回答を判断してください。
最終回答は日本語で簡潔にまとめてください。
ファイル操作はワークスペース内だけを対象にし、秘密情報を読み取らないでください。
外部送信・上書き・削除・危険なコマンドは、必要性と安全性を確認してから扱ってください。
"""


@dataclass
class ExecutionEvent:
    """自律ループの 1 回の Action / Observation を監査可能な形で表す。"""

    step: int
    action: str
    arguments: dict[str, Any]
    observation: Any
    duration_ms: float


@dataclass
class AgentResult:
    text: str
    steps: int
    tool_calls: int
    trace: list[ExecutionEvent] = field(default_factory=list)


def get_client() -> Any:
    """GEMINI_API_KEY から Gemini クライアントを作成する。"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-api-key":
        raise ValueError("GEMINI_API_KEY is not set in environment variables.")
    return genai.Client(api_key=api_key)


_RETRYABLE_ERRORS = (errors.APIError, TimeoutError, socket.timeout)


class AgentCore:
    """Gemini の判断とツール実行を繰り返す Thought–Action–Observation ループ。"""

    def __init__(
        self,
        client: Any | None = None,
        model_id: str | None = None,
        registry: ToolRegistry | None = None,
        max_steps: int = 8,
        max_retries: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client if client is not None else get_client()
        self.model_id = model_id or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        self.registry = registry or build_default_registry()
        self.max_steps = max(1, max_steps)
        self.max_retries = max(1, max_retries)
        self.sleep_fn = sleep_fn

    def _config(self) -> types.GenerateContentConfig:
        declarations = [tool.declaration for tool in self.registry.tools.values()]
        return types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0.2,
            tools=[types.Tool(function_declarations=declarations)],
        )

    @staticmethod
    def _parts(response: Any) -> list[Any]:
        try:
            return list(response.candidates[0].content.parts or [])
        except (AttributeError, IndexError, TypeError):
            return []

    def _generate_content_with_retry(self, contents: list[Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self.client.models.generate_content(
                    model=self.model_id,
                    contents=contents,
                    config=self._config(),
                )
            except _RETRYABLE_ERRORS as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    self.sleep_fn(2**attempt)
            except Exception:
                raise
        assert last_error is not None
        raise last_error

    def run(self, prompt: str) -> AgentResult:
        if not prompt.strip():
            raise ValueError("prompt は空にできません")
        contents: list[Any] = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        trace: list[ExecutionEvent] = []
        tool_calls = 0

        for step in range(1, self.max_steps + 1):
            try:
                response = self._generate_content_with_retry(contents)
            except _RETRYABLE_ERRORS as exc:
                return AgentResult(f"[Error] Gemini API/Timeout Error: {exc}", step, tool_calls, trace)
            except Exception as exc:
                return AgentResult(f"[Error] Unexpected Error: {exc}", step, tool_calls, trace)

            parts = self._parts(response)
            if not parts:
                return AgentResult("モデルから有効な応答が返りませんでした。", step, tool_calls, trace)
            contents.append(response.candidates[0].content)
            calls = [part.function_call for part in parts if getattr(part, "function_call", None)]
            if not calls:
                text = "".join(part.text for part in parts if getattr(part, "text", None)).strip()
                return AgentResult(text or "回答を生成できませんでした。", step, tool_calls, trace)

            function_parts: list[Any] = []
            for call in calls:
                tool_calls += 1
                name = call.name
                args = dict(call.args or {})
                started = time.perf_counter()
                result = self.registry.execute(name, args)
                duration_ms = (time.perf_counter() - started) * 1000
                trace.append(ExecutionEvent(step, name, args, result, duration_ms))
                function_parts.append(
                    types.Part.from_function_response(name=name, response={"result": result})
                )
            # Gemini GenerateContent API では FunctionResponse を user role の
            # Content として返す（tool role は Live API 用の形式）。
            contents.append(types.Content(role="user", parts=function_parts))

        return AgentResult(
            f"ツール実行が上限（{self.max_steps} ステップ）に達しました。途中結果を確認してください。",
            self.max_steps,
            tool_calls,
            trace,
        )


def run_agent_core(prompt: str, max_retries: int = 3) -> str:
    try:
        return AgentCore(max_retries=max_retries).run(prompt).text
    except Exception as exc:
        return f"[Error] Unexpected Error: {exc}"


def run_agent(prompt: str, max_retries: int = 3) -> str:
    return run_agent_core(prompt, max_retries=max_retries)
