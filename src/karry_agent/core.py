"""Gemini Function Calling エージェントのコア実装。"""
from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from .tools import ToolRegistry, build_default_registry

load_dotenv()

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


def get_client() -> Any:
    """環境変数から Gemini クライアントを作成する。"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your-gemini-api-key":
        raise ValueError("GEMINI_API_KEY is not set in environment variables.")
    return genai.Client(api_key=api_key)


# SDK の APIError に加え、標準ライブラリのタイムアウトも再試行対象にする。
_RETRYABLE_ERRORS = (errors.APIError, TimeoutError, socket.timeout)


class AgentCore:
    """Gemini の思考・ツール実行ループを管理する。"""

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
        self.max_steps = max_steps
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
        """API 呼び出しを指数バックオフ付きで再試行する。"""
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
                if attempt >= self.max_retries - 1:
                    break
                self.sleep_fn(2**attempt)
            except Exception:
                # 認証・設定・プログラムエラーは再試行せず、呼び出し元で明示的に扱う。
                raise
        assert last_error is not None
        raise last_error

    def run(self, prompt: str) -> AgentResult:
        if not prompt.strip():
            raise ValueError("prompt は空にできません")
        contents: list[Any] = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
        tool_calls = 0
        for step in range(1, self.max_steps + 1):
            try:
                response = self._generate_content_with_retry(contents)
            except _RETRYABLE_ERRORS as exc:
                return AgentResult(f"[Error] Gemini API/Timeout Error: {exc}", step, tool_calls)
            except Exception as exc:
                return AgentResult(f"[Error] Unexpected Error: {exc}", step, tool_calls)

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

        return AgentResult(
            f"ツール実行が上限（{self.max_steps} ステップ）に達しました。途中結果を確認してください。",
            self.max_steps,
            tool_calls,
        )


def run_agent_core(prompt: str, max_retries: int = 3) -> str:
    """後方互換用の簡易 API。初期化・実行エラーを文字列で返す。"""
    try:
        return AgentCore(max_retries=max_retries).run(prompt).text
    except Exception as exc:
        return f"[Error] Unexpected Error: {exc}"


def run_agent(prompt: str, max_retries: int = 3) -> str:
    """Phase 2/3 CLI 互換 API。"""
    return run_agent_core(prompt, max_retries=max_retries)
