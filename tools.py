"""Phase 4 の外部ツール定義（CLI・Function Calling から利用可能）。"""
from __future__ import annotations

import datetime


def get_current_datetime() -> str:
    """現在の日時を ``YYYY-MM-DD HH:MM:SS`` 形式で返す。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def web_search_mock(query: str) -> str:
    """最新情報検索の接続先を差し替えるための安全なモック。"""
    return f"[Search Results for '{query}']: Simulated search response with relevant web information."


# エージェントに登録するツール一覧
AGENT_TOOLS = [get_current_datetime, web_search_mock]

__all__ = ["get_current_datetime", "web_search_mock", "AGENT_TOOLS"]
