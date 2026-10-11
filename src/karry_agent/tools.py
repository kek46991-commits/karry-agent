"""Built-in tools exposed to Gemini function calling."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass
class RegisteredTool:
    declaration: dict[str, Any]
    handler: Callable[..., Any]


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, RegisteredTool] = {}

    def register(self, name: str, description: str, parameters: dict[str, Any], handler: Callable[..., Any]) -> None:
        self.tools[name] = RegisteredTool(
            declaration={"name": name, "description": description, "parameters": parameters},
            handler=handler,
        )

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if not tool:
            return {"error": f"unknown tool: {name}"}
        try:
            return tool.handler(**args)
        except Exception as exc:  # ツールエラーをモデルへ返し、ループを継続可能にする
            return {"error": f"{type(exc).__name__}: {exc}"}


def _safe_path(path: str) -> Path:
    root = Path(os.getenv("KARRY_WORKSPACE", os.getcwd())).resolve()
    target = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if root not in target.parents and target != root:
        raise PermissionError("workspace 外のパスは操作できません")
    return target


def read_file(path: str) -> dict[str, Any]:
    target = _safe_path(path)
    if not target.is_file():
        return {"error": "file not found", "path": str(target)}
    content = target.read_text(encoding="utf-8")
    return {"path": str(target), "content": content[:100_000], "truncated": len(content) > 100_000}


def write_file(path: str, content: str) -> dict[str, Any]:
    target = _safe_path(path)
    if target.exists():
        return {"error": "refusing to overwrite existing file; ask the user first", "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": target.stat().st_size}


def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """DuckDuckGo Instant Answer API。外部サービスへの送信は検索語だけ。"""
    if not query.strip():
        return {"error": "query is empty"}
    url = "https://api.duckduckgo.com/?q=" + quote(query) + "&format=json&no_html=1&skip_disambig=1"
    request = Request(url, headers={"User-Agent": "karry-agent/0.1"})
    with urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    results = []
    if data.get("AbstractText"):
        results.append({"title": data.get("Heading", query), "snippet": data["AbstractText"], "url": data.get("AbstractURL")})
    for topic in data.get("RelatedTopics", [])[:max_results]:
        if "Text" in topic:
            results.append({"title": topic.get("Text", "")[:120], "snippet": topic.get("Text", ""), "url": topic.get("FirstURL")})
    return {"query": query, "results": results[:max_results]}


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    string_param = {"type": "string"}
    registry.register("read_file", "ワークスペース内の UTF-8 ファイルを読み取る", {"type": "object", "properties": {"path": string_param}, "required": ["path"]}, read_file)
    registry.register("write_file", "新規ファイルをワークスペース内に作成する。既存ファイルは上書きしない", {"type": "object", "properties": {"path": string_param, "content": string_param}, "required": ["path", "content"]}, write_file)
    registry.register("web_search", "Web を検索し、概要と URL を返す", {"type": "object", "properties": {"query": string_param, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}, web_search)
    return registry
