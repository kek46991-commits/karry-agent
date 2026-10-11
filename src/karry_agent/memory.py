"""エージェントの会話履歴を保持・管理するメモリ。"""
from __future__ import annotations

from typing import Any


class ConversationMemory:
    """ユーザーとモデルのメッセージ履歴を最大件数内で保持する。"""

    def __init__(self, max_history: int = 10):
        if max_history < 1:
            raise ValueError("max_history は 1 以上で指定してください")
        self.max_history = max_history
        self.history: list[dict[str, Any]] = []

    def add_message(self, role: str, content: str) -> None:
        if not role.strip():
            raise ValueError("role は空にできません")
        self.history.append({"role": role, "content": content})
        if len(self.history) > self.max_history * 2:
            self.history = self.history[-(self.max_history * 2):]

    def get_history(self) -> list[dict[str, Any]]:
        """現在の履歴のコピーを返す。呼び出し側から直接変更されない。"""
        return list(self.history)

    def clear(self) -> None:
        self.history.clear()
