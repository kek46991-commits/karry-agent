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
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from karry_agent.calendar_secrets import (
    CREDENTIALS_ENV,
    TOKEN_ENV,
    CalendarSecretError,
    parse_secret_json,
    to_env_value,
    validate_credentials,
    validate_token,
)


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
    return {"query": query, "results": results[:max_results], "message": "検索結果がありません" if not results else None}


_WEATHER_CODES = {
    0: "快晴", 1: "晴れ", 2: "一部曇り", 3: "曇り",
    45: "霧", 48: "着氷性の霧", 51: "弱い霧雨", 53: "霧雨",
    55: "強い霧雨", 56: "弱い着氷性の霧雨", 57: "強い着氷性の霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨", 66: "弱い着氷性の雨",
    67: "強い着氷性の雨", 71: "弱い雪", 73: "雪", 75: "強い雪",
    77: "雪あられ", 80: "弱いにわか雨", 81: "にわか雨", 82: "強いにわか雨",
    85: "弱いにわか雪", 86: "強いにわか雪", 95: "雷雨", 96: "雷雨と弱い雹",
    99: "雷雨と強い雹",
}


def get_weather(location: str) -> dict[str, Any]:
    """Open-Meteoで指定地域の現在天気を取得する（APIキー不要）。"""
    if not location or not location.strip():
        return {"error": "location is required"}
    try:
        geocode_url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
            "name": location.strip(), "count": 1, "language": "ja", "format": "json",
        })
        request = Request(geocode_url, headers={"User-Agent": "karry-agent/0.1"})
        with urlopen(request, timeout=10) as response:
            places = json.loads(response.read().decode("utf-8")).get("results", [])
        if not places:
            return {"error": f"地域が見つかりません: {location.strip()}"}
        place = places[0]
        forecast_url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "auto",
        })
        request = Request(forecast_url, headers={"User-Agent": "karry-agent/0.1"})
        with urlopen(request, timeout=10) as response:
            forecast = json.loads(response.read().decode("utf-8"))
        current = forecast.get("current", {})
        code = current.get("weather_code")
        return {
            "location": place.get("name", location.strip()),
            "country": place.get("country"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "timezone": forecast.get("timezone"),
            "current": {
                "time": current.get("time"),
                "condition": _WEATHER_CODES.get(code, f"天気コード {code}"),
                "temperature_c": current.get("temperature_2m"),
                "apparent_temperature_c": current.get("apparent_temperature"),
                "relative_humidity_percent": current.get("relative_humidity_2m"),
                "wind_speed_kmh": current.get("wind_speed_10m"),
            },
            "source": "Open-Meteo",
        }
    except Exception as exc:
        return {"error": f"天気APIの取得に失敗しました: {type(exc).__name__}: {exc}"}


_RESTORED_CALENDAR_SECRETS: set[str] = set()


def _restore_calendar_secret(env_name: str, target: Path) -> None:
    """ヘッドレス環境の Secret JSON を正規化し、プロセス起動後最初の 1 回だけファイルへ復元する。"""
    raw = os.getenv(env_name)
    if not raw or env_name in _RESTORED_CALENDAR_SECRETS:
        return
    try:
        data = parse_secret_json(raw, env_name)
        if env_name == CREDENTIALS_ENV:
            validate_credentials(data)
        else:
            credentials_raw = os.getenv(CREDENTIALS_ENV)
            credentials = None
            if credentials_raw:
                try:
                    candidate = parse_secret_json(credentials_raw, CREDENTIALS_ENV)
                    credentials = validate_credentials(candidate)
                except CalendarSecretError:
                    # authorized_user token に client 情報が含まれる本番構成では、
                    # 補助的な credentials Secret の不正形式で token 復元を止めない。
                    credentials = None
            data = validate_token(data, credentials)
    except CalendarSecretError as exc:
        raise RuntimeError(str(exc)) from None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_env_value(data), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    _RESTORED_CALENDAR_SECRETS.add(env_name)


def _calendar_service() -> Any:
    """既存 token を優先して Google Calendar API client を作成する。"""
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google Calendar dependencies are not installed") from exc

    scopes = ["https://www.googleapis.com/auth/calendar.events"]
    token_path = Path(os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "token.json"))
    credentials_path = Path(os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "credentials.json"))
    # Production では token JSON に client_id / client_secret が含まれるため、
    # credentials JSON が未設定・形式不正でも token の refresh を先に試す。
    # 先に credentials を復元すると、使える token があるのに無関係な
    # credentials Secret の形式エラーで insert/list 全体が失敗していた。
    _restore_calendar_secret(TOKEN_ENV, token_path)
    creds = None
    if token_path.is_file():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(GoogleRequest())
        except Exception:
            # Refresh が必要で token に client 情報がない場合だけ、下の
            # credentials/OAuth フォールバックへ進む。
            creds = None
    if not creds or not creds.valid:
        _restore_calendar_secret(CREDENTIALS_ENV, credentials_path)
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


def _calendar_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    """Google Calendar イベントからエージェントに返す安全な要約を作る。"""
    return {
        "event_id": event.get("id"),
        "summary": event.get("summary", ""),
        "start": event.get("start", {}),
        "end": event.get("end", {}),
        "status": event.get("status"),
        "html_link": event.get("htmlLink"),
    }


def list_calendar_events(
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Google Calendar の予定を読み取り、予定の要約一覧を返す。"""
    if not 1 <= max_results <= 100:
        return {"error": "max_results must be between 1 and 100"}

    params: dict[str, Any] = {
        "calendarId": "primary",
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }
    for name, value in (("timeMin", time_min), ("timeMax", time_max)):
        if value is None:
            continue
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return {"error": f"{name} must be ISO 8601 with timezone offset"}
        if parsed.tzinfo is None:
            return {"error": f"{name} must include a timezone offset"}
        params[name] = parsed.isoformat()
    if query and query.strip():
        params["q"] = query.strip()

    response = _calendar_service().events().list(**params).execute()
    events = [_calendar_event_summary(event) for event in response.get("items", [])]
    return {"events": events, "count": len(events), "next_page_token": response.get("nextPageToken")}


def update_calendar_event(
    event_id: str,
    summary: str | None = None,
    start_time: str | None = None,
    duration_minutes: int | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    """予定変更をプレビューし、confirmed=True のときだけ Google Calendar に反映する。"""
    if not event_id.strip():
        return {"error": "event_id is required"}
    if summary is None and start_time is None and duration_minutes is None:
        return {"error": "at least one field to update is required"}
    if summary is not None and not summary.strip():
        return {"error": "summary must not be empty"}
    if duration_minutes is not None and not 1 <= duration_minutes <= 1_440:
        return {"error": "duration_minutes must be between 1 and 1440"}

    service = _calendar_service()
    current = service.events().get(calendarId="primary", eventId=event_id).execute()
    current_start = current.get("start", {}).get("dateTime")
    current_end = current.get("end", {}).get("dateTime")
    patch: dict[str, Any] = {}
    if summary is not None:
        patch["summary"] = summary.strip()

    if start_time is not None or duration_minutes is not None:
        if start_time is None:
            start_value = current_start
        else:
            start_value = start_time
        try:
            start = datetime.datetime.fromisoformat(start_value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return {"error": "start_time must be ISO 8601 with timezone offset"}
        if start.tzinfo is None:
            return {"error": "start_time must include a timezone offset"}
        if duration_minutes is None:
            try:
                end = datetime.datetime.fromisoformat(current_end.replace("Z", "+00:00"))
                duration_minutes = max(1, int((end - datetime.datetime.fromisoformat(current_start.replace("Z", "+00:00"))).total_seconds() / 60))
            except (AttributeError, ValueError, TypeError):
                return {"error": "existing event has no usable dateTime range"}
        end = start + datetime.timedelta(minutes=duration_minutes)
        patch["start"] = {"dateTime": start.isoformat(), "timeZone": "Asia/Tokyo"}
        patch["end"] = {"dateTime": end.isoformat(), "timeZone": "Asia/Tokyo"}

    preview = {**_calendar_event_summary(current), "changes": patch}
    if not confirmed:
        return {"confirmation_required": True, "event_preview": preview}

    result = service.events().patch(calendarId="primary", eventId=event_id, body=patch).execute()
    return {"updated": True, **_calendar_event_summary(result)}


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
        "get_weather",
        "指定地域の現在の天気をOpen-Meteoから取得する。天気、気温、湿度、風速を尋ねられたら使う",
        {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "市区町村名。例: 枚方市"}},
            "required": ["location"],
        },
        get_weather,
    )
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
    registry.register(
        "list_calendar_events",
        "Google Calendar の予定を検索・取得する（読み取り専用）",
        {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "ISO 8601 with timezone offset"},
                "time_max": {"type": "string", "description": "ISO 8601 with timezone offset"},
                "query": string_param,
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
        list_calendar_events,
    )
    registry.register(
        "update_calendar_event",
        "Google Calendar の予定変更をプレビューする。実際の変更には confirmed=true が必要",
        {
            "type": "object",
            "properties": {
                "event_id": string_param,
                "summary": string_param,
                "start_time": {"type": "string", "description": "ISO 8601 with timezone offset"},
                "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                "confirmed": {"type": "boolean", "description": "ユーザーが変更内容を明示確認した場合のみ true"},
            },
            "required": ["event_id"],
        },
        update_calendar_event,
    )
    return registry
