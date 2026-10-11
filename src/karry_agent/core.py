"""Gemini function-calling agent loop."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from .tools import ToolRegistry, build_default_registry

SYSTEM_INSTRUCTION = """あなたは karry-agent の実行コアです。
ユーザーの依頼を達成するため、必要なら利用可能なツールを呼び出してください。
ツールの結果を確認してから次の行動を判断し、最後は日本語で簡潔に回答してください。
ファイル操作はユーザーが指定したパスだけを対象にし、パス逸脱や秘密情報の読み取りを避けてください。
不明な点や危険な操作（削除・上書き・外部送信）は、実行前に確認してください。
"""


@dataclass
class AgentResult:
    text: str
    steps: int
    tool_calls: int


class AgentCore:
    """Gemini の思考・ツール実行ループを管理する。"""

    def __init__(self, client: Any | None = None, model_id: str | None = None, registry: ToolRegistry | None = None, max_steps: int = 8) -> None:
        if client is None:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY が設定されていません。.env.example を参照してください。")
            client = genai.Client(api_key=api_key)
        self.client = client
        self.model_id = model_id or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.registry = registry or build_default_registry()
        self.max_steps = max_steps

    def _config(self) -> types.GenerateContentConfig:
        declarations = [tool.declaration for tool in self.registry.tools.values()]
        return types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.2, tools=[types.Tool(function_declarations=declarations)])

    @staticmethod
    def _parts(response: Any) -> list[Any]:
        try:
            return list(response.candidates[0].content.parts or [])
        except (AttributeError, IndexError, TypeError):
            return []

    def run(self, prompt: str) -> AgentResult:
        if not prompt.strip():
            raise ValueError("prompt は空にできません")
        contents: list[Any] = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        tool_calls = 0
        for step in range(1, self.max_steps + 1):
            response = self.client.models.generate_content(model=self.model_id, contents=contents, config=self._config())
            parts = self._parts(response)
            if not parts:
                return AgentResult("モデルから有効な応答が返りませんでした。", step, tool_calls)
            contents.append(response.candidates[0].content)
            calls = [part.function_call for part in parts if getattr(part, "function_call", None)]
            if not calls:
                text = "".join(part.text for part in parts if getattr(part, "text", None)).strip()
                return AgentResult(text or "回答を生成できませんでした。", step, tool_calls)
            function_parts: list[Any] = []
            for call in calls:
                tool_calls += 1
                result = self.registry.execute(call.name, dict(call.args or {}))
                function_parts.append(types.Part.from_function_response(name=call.name, response={"result": result}))
            contents.append(types.Content(role="tool", parts=function_parts))
        return AgentResult(f"ツール実行が上限（{self.max_steps} ステップ）に達しました。途中結果を確認してください。", self.max_steps, tool_calls)


def run_agent_core(prompt: str) -> str:
    return AgentCore().run(prompt).text


def run_agent(prompt: str) -> str:
    """Phase 2 CLI 互換 API。"""
    return run_agent_core(prompt)
