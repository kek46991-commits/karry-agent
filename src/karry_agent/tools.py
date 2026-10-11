"""Built-in tools exposed to Gemini function calling."""
from __future__ import annotations

import datetime
import json
import os
import shlex
import subprocess
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
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


def get_current_datetime() -> str:
    """現在の日時を ``YYYY-MM-DD HH:MM:SS`` 形式で返す。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def web_search_mock(query: str) -> str:
    """最新情報検索の接続先を差し替えるための安全なモック。"""
    return f"[Search Results for '{query}']: Simulated search response with relevant web information."


def generate_image_mock(prompt: str) -> str:
    """テキストプロンプトに基づく画像生成の安全なモック。"""
    return f"[Image Generated]: Successfully generated image for prompt '{prompt}' (mock_image_path.png)"


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


def browse_path(path: str = ".", max_entries: int = 100) -> dict[str, Any]:
    """ファイルまたはディレクトリを閲覧する。内容は UTF-8 の先頭だけ返す。"""
    try:
        target = _safe_path(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if target.is_file():
        content = target.read_text(encoding="utf-8")
        return {"path": str(target), "type": "file", "content": content[:100_000], "truncated": len(content) > 100_000}
    if target.is_dir():
        return list_workspace(path, max_entries)
    return {"error": "path not found", "path": str(target)}


def write_file(path: str, content: str) -> dict[str, Any]:
    target = _safe_path(path)
    if target.exists():
        return {"error": "refusing to overwrite existing file; ask the user first", "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": target.stat().st_size}


def append_text_file(path: str, content: str) -> dict[str, Any]:
    """ワークスペース内の UTF-8 テキストファイルへ追記する。"""
    try:
        target = _safe_path(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if target.exists() and not target.is_file():
        return {"error": "path is not a file", "path": str(target)}
    if target.exists() and target.stat().st_size > 1_000_000:
        return {"error": "file is too large to append safely", "path": str(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return {"path": str(target), "bytes_appended": len(content.encode("utf-8")), "size": target.stat().st_size}


def list_workspace(path: str = ".", max_entries: int = 100) -> dict[str, Any]:
    """ワークスペース内のファイル一覧を返す（隠しファイルと秘密ファイルを除外）。"""
    try:
        target = _safe_path(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if not target.is_dir():
        return {"error": "directory not found", "path": str(target)}
    if not 1 <= max_entries <= 500:
        return {"error": "max_entries must be between 1 and 500"}
    entries = []
    for item in sorted(target.rglob("*")):
        if any(part.startswith(".") for part in item.relative_to(target).parts):
            continue
        entries.append({
            "path": str(item.relative_to(target)),
            "type": "directory" if item.is_dir() else "file",
        })
        if len(entries) >= max_entries:
            break
    return {"path": str(target), "entries": entries, "truncated": len(entries) >= max_entries}


_ALLOWED_COMMANDS = {"pwd", "ls", "find", "grep", "cat", "git", "python", "python3"}
_BLOCKED_TOKENS = {"-c", "--command", "sh", "bash", "zsh", "rm", "sudo", "curl", "wget"}


def run_command(command: str, timeout: int = 10) -> dict[str, Any]:
    """ワークスペース内の読み取り系コマンドだけを shell 無しで実行する。"""
    if not command.strip():
        return {"error": "command is empty"}
    if any(char in command for char in (";", "|", "&", ">", "<", "`", "\n")):
        return {"error": "shell operators are not allowed"}
    argv = shlex.split(command)
    if not argv or argv[0] not in _ALLOWED_COMMANDS or any(token in _BLOCKED_TOKENS for token in argv):
        return {"error": "command is not in the read-only allowlist"}
    if not 1 <= timeout <= 30:
        return {"error": "timeout must be between 1 and 30 seconds"}
    completed = subprocess.run(
        argv,
        cwd=_safe_path("."),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
        env={"PATH": os.getenv("PATH", ""), "LANG": "C.UTF-8"},
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20_000:],
        "stderr": completed.stderr[-20_000:],
    }


def run_local_script(path: str, args: list[str] | None = None, timeout: int = 30) -> dict[str, Any]:
    """ワークスペース内の Python スクリプトを shell 無しで実行する。"""
    try:
        script = _safe_path(path)
    except PermissionError as exc:
        return {"error": str(exc)}
    if script.suffix != ".py" or not script.is_file():
        return {"error": "only an existing .py script inside the workspace can be executed"}
    if not 1 <= timeout <= 30:
        return {"error": "timeout must be between 1 and 30 seconds"}
    script_args = [str(value) for value in (args or [])]
    if any(any(char in value for char in (";", "|", "&", ">", "<", "`", "\n")) for value in script_args):
        return {"error": "shell operators are not allowed in script arguments"}
    completed = subprocess.run(
        ["python3", str(script), *script_args],
        cwd=script.parent,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
        env={"PATH": os.getenv("PATH", ""), "LANG": "C.UTF-8"},
    )
    return {
        "script": str(script),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20_000:],
        "stderr": completed.stderr[-20_000:],
    }


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


def _calendar_service() -> Any:
    """既存 token を優先して Google Calendar API client を作成する。"""
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google Calendar dependencies are not installed") from exc

    scopes = ["https://www.googleapis.com/auth/calendar"]
    token_path = Path(os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "token.json"))
    credentials_path = Path(os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "credentials.json"))
    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
    elif not creds or not creds.valid:
        if not credentials_path.is_file():
            raise FileNotFoundError(
                "Google Calendar の認証が未設定です。GOOGLE_CALENDAR_TOKEN_FILE または credentials.json を設定してください。"
            )
        if os.getenv("GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH") != "1":
            raise RuntimeError(
                "Google Calendar の既存 token がありません。ローカルで GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH=1 を設定して OAuth を完了してください。"
            )
        from google_auth_oauthlib.flow import InstalledAppFlow
        creds = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes).run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return build("calendar", "v3", credentials=creds)


def add_calendar_event(
    summary: str,
    start_time: str,
    duration_minutes: int = 60,
    confirmed: bool = False,
) -> dict[str, Any]:
    """予定をプレビューし、confirmed=True のときだけ Google Calendar に登録する。"""
    if not summary.strip():
        return {"error": "summary is required"}
    if not 1 <= duration_minutes <= 1_440:
        return {"error": "duration_minutes must be between 1 and 1440"}
    try:
        start = datetime.datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except ValueError:
        return {"error": "start_time must be ISO 8601, for example 2026-10-12T10:00:00+09:00"}
    if start.tzinfo is None:
        return {"error": "start_time must include a timezone offset"}
    end = start + datetime.timedelta(minutes=duration_minutes)
    event = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Tokyo"},
    }
    if not confirmed:
        return {"confirmation_required": True, "event_preview": event}
    service = _calendar_service()
    result = service.events().insert(calendarId="primary", body=event).execute()
    return {"created": True, "event_id": result.get("id"), "html_link": result.get("htmlLink"), "event": event}


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    string_param = {"type": "string"}
    registry.register("get_current_datetime", "現在の日時を取得する", {"type": "object", "properties": {}}, get_current_datetime)
    registry.register("web_search_mock", "Web 検索のモックを実行する", {"type": "object", "properties": {"query": string_param}, "required": ["query"]}, web_search_mock)
    registry.register("generate_image_mock", "画像生成のモックを実行する", {"type": "object", "properties": {"prompt": string_param}, "required": ["prompt"]}, generate_image_mock)
    registry.register("read_file", "ワークスペース内の UTF-8 ファイルを読み取る", {"type": "object", "properties": {"path": string_param}, "required": ["path"]}, read_file)
    registry.register("browse_path", "ワークスペース内のファイルまたはディレクトリを閲覧する", {"type": "object", "properties": {"path": string_param, "max_entries": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": []}, browse_path)
    registry.register("write_file", "新規ファイルをワークスペース内に作成する。既存ファイルは上書きしない", {"type": "object", "properties": {"path": string_param, "content": string_param}, "required": ["path", "content"]}, write_file)
    registry.register("append_text_file", "ワークスペース内の UTF-8 テキストファイルへ追記する", {"type": "object", "properties": {"path": string_param, "content": string_param}, "required": ["path", "content"]}, append_text_file)
    registry.register("list_workspace", "ワークスペース内のファイル一覧を取得する", {"type": "object", "properties": {"path": string_param, "max_entries": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": []}, list_workspace)
    registry.register("run_command", "ワークスペース内で安全な読み取り系コマンドを実行する", {"type": "object", "properties": {"command": string_param, "timeout": {"type": "integer", "minimum": 1, "maximum": 30}}, "required": ["command"]}, run_command)
    registry.register("run_local_script", "ワークスペース内の Python スクリプトを実行する", {"type": "object", "properties": {"path": string_param, "args": {"type": "array", "items": string_param}, "timeout": {"type": "integer", "minimum": 1, "maximum": 30}}, "required": ["path"]}, run_local_script)
    registry.register("web_search", "Web を検索し、概要と URL を返す", {"type": "object", "properties": {"query": string_param, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}, web_search)
    registry.register(
        "add_calendar_event",
        "Google Calendar の予定をプレビューする。実際の登録には confirmed=true が必要",
        {
            "type": "object",
            "properties": {
                "summary": string_param,
                "start_time": {"type": "string", "description": "ISO 8601 with timezone offset"},
                "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "confirmed": {"type": "boolean", "description": "ユーザーが登録内容を明示確認した場合のみ true"},
            },
            "required": ["summary", "start_time"],
        },
        add_calendar_event,
    )
    return registry
