"""Built-in tools exposed to Gemini function calling."""
from __future__ import annotations

import datetime
import inspect
import json
import os
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from karry_agent.self_healing import healing_engine
from karry_agent.calendar_secrets import (
    CREDENTIALS_ENV,
    TOKEN_ENV,
    CalendarSecretError,
    parse_secret_json,
    to_env_value,
    validate_credentials,
    validate_token,
)

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")


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
        result = self._execute(name, args)
        if isinstance(result, dict) and "healing_status" not in result:
            healing = healing_engine.observe_result(result, {"source": "tool_registry", "tool_name": name})
            if healing and name in self.tools:
                result = {**result, "healing_status": healing}
        return result

    def _execute(self, name: str, args: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if not tool:
            return {"error": f"unknown tool: {name}"}
        try:
            if not isinstance(args, dict):
                return {"status": "error", "error": "arguments must be an object"}
            required = tool.declaration.get("parameters", {}).get("required", [])
            if any(key not in args for key in required):
                return {"status": "error", "error": "required tool arguments are missing"}
            inspect.signature(tool.handler).bind(**args)
            properties = tool.declaration.get("parameters", {}).get("properties", {})
            for key, value in args.items():
                expected = properties.get(key, {}).get("type")
                valid = {"string": isinstance(value, str), "boolean": isinstance(value, bool),
                         "integer": isinstance(value, int) and not isinstance(value, bool),
                         "array": isinstance(value, list), "object": isinstance(value, dict)}
                if expected in valid and not valid[expected]:
                    return {"status": "error", "error": f"invalid argument type: {key}"}
            return tool.handler(**args)
        except Exception as exc:
            healing = healing_engine.analyze_and_heal(exc, {"source": "tool_registry", "tool_name": name})
            return {"status": "error", "error": f"{type(exc).__name__}: tool execution failed",
                    "healing_status": healing}


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
    """CalendarとPhotosで共通のOAuth取得・refresh処理を利用する。"""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    from .google_workspace import CALENDAR_SCOPE, get_google_credentials

    creds = get_google_credentials([CALENDAR_SCOPE])
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build("calendar", "v3", http=http, cache_discovery=False)


def _parse_calendar_start_time(value: str) -> datetime.datetime:
    """ISO日時または日本語の相対日時をAsia/Tokyoのaware datetimeへ変換する。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("start_time is required")
    raw = value.strip()
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(JST)
    except ValueError:
        pass

    normalized = raw.lower().replace("：", ":").replace("　", " ").strip()
    day_offset = None
    for word, offset in (("明日", 1), ("あした", 1), ("tomorrow", 1), ("今日", 0), ("きょう", 0), ("today", 0)):
        if word in normalized:
            day_offset = offset
            normalized = normalized.replace(word, "", 1).strip()
            break
    if day_offset is None:
        raise ValueError("start_time must be ISO 8601 or a relative time such as 明日の10:00")

    match = re.search(r"(\d{1,2})(?::|時)(\d{1,2})?分?", normalized)
    if not match:
        raise ValueError("relative start_time must include an hour, such as 明日の10:00")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError("start_time hour/minute is out of range")
    base = datetime.datetime.now(JST) + datetime.timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def add_calendar_event(
    summary: str,
    start_time: str,
    duration_minutes: int = 60,
    confirmed: bool = False,
    *,
    end_time: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    """予定をプレビューし、confirmed=True のときだけ Google Calendar に登録する。"""
    if not isinstance(summary, str) or not summary.strip():
        return {"error": "summary is required"}
    try:
        duration_minutes = int(duration_minutes)
    except (TypeError, ValueError):
        return {"error": "duration_minutes must be an integer between 1 and 1440"}
    if not 1 <= duration_minutes <= 1_440:
        return {"error": "duration_minutes must be between 1 and 1440"}
    try:
        start = _parse_calendar_start_time(start_time)
        end = _parse_calendar_start_time(end_time) if end_time is not None else start + datetime.timedelta(minutes=duration_minutes)
        if end <= start:
            return {"error": "end_time must be after start_time"}
    except (TypeError, ValueError) as exc:
        return {"error": str(exc)}
    event = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Tokyo"},
    }
    if description:
        event["description"] = description
    if not confirmed:
        return {"status": "needs_confirmation", "confirmation_required": True, "event_preview": event}
    try:
        service = _calendar_service()
        result = service.events().insert(calendarId="primary", body=event).execute()
    except Exception as exc:
        logger.exception("Google Calendar event insert failed")
        message = "カレンダー連携の認証が必要です。Calendar tokenとcalendar.eventsスコープを確認してください。"
        if "credential" not in type(exc).__name__.lower() and "auth" not in str(exc).lower() and "401" not in str(exc) and "403" not in str(exc):
            message = "Google Calendarへの予定登録に失敗しました。日時・認証・API設定を確認してください。"
        from .google_workspace import google_error
        return {**google_error(exc, "Google_API_Error"), "created": False, "error": message}
    return {"status": "success", "created": True, "event_id": result.get("id"), "html_link": result.get("htmlLink"), "htmlLink": result.get("htmlLink"), "event": event}


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
    from . import agent_core as integrated
    from .url_analysis import fetch_and_summarize_url
    from .instant_website import prepare_instant_website

    registry = ToolRegistry()
    string_param = {"type": "string"}
    registry.register("get_current_datetime", "現在の日時を取得する", {"type": "object", "properties": {}}, get_current_datetime)
    registry.register("web_search_mock", "Web 検索のモックを実行する", {"type": "object", "properties": {"query": string_param}, "required": ["query"]}, web_search_mock)
    registry.register("generate_image_mock", "画像生成のモックを実行する", {"type": "object", "properties": {"prompt": string_param}, "required": ["prompt"]}, generate_image_mock)
    registry.register("browse_path", "ワークスペース内のファイルまたはディレクトリを閲覧する", {"type": "object", "properties": {"path": string_param, "max_entries": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": []}, browse_path)
    registry.register("append_text_file", "ワークスペース内の UTF-8 テキストファイルへ追記する", {"type": "object", "properties": {"path": string_param, "content": string_param}, "required": ["path", "content"]}, append_text_file)
    registry.register("list_workspace", "ワークスペース内のファイル一覧を取得する", {"type": "object", "properties": {"path": string_param, "max_entries": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": []}, list_workspace)
    registry.register("run_command", "ワークスペース内で安全な読み取り系コマンドを実行する", {"type": "object", "properties": {"command": string_param, "timeout": {"type": "integer", "minimum": 1, "maximum": 30}}, "required": ["command"]}, run_command)
    registry.register("run_local_script", "ワークスペース内の Python スクリプトを実行する", {"type": "object", "properties": {"path": string_param, "args": {"type": "array", "items": string_param}, "timeout": {"type": "integer", "minimum": 1, "maximum": 30}}, "required": ["path"]}, run_local_script)
    registry.register("web_search", "Web を検索し、概要と URL を返す", {"type": "object", "properties": {"query": string_param, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}, web_search)
    registry.register("fetch_and_summarize_url", "公開URLのページを1回取得し、タイトル・本文3000文字・見出し・構造を抽出する。URLの解析・改善提案は検索や再取得を繰り返さずこの結果から回答する", {"type": "object", "properties": {"url": string_param}, "required": ["url"]}, fetch_and_summarize_url)
    registry.register("deploy_instant_website", "HTML成果物を即時URL公開するための確認内容を準備する。生成HTML全文と公開先をユーザーへ提示し、所有者が確認後に公開する。GitHubや追加設定は不要。一時保存のため再起動で消える", {"type": "object", "properties": {"html_content": string_param, "filename": string_param}, "required": ["html_content"]}, prepare_instant_website)
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
                "end_time": {"type": "string", "description": "終了時刻。未指定ならduration_minutesを使用"},
                "description": string_param,
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
    # Both filepath (supplied interface) and path (existing model/API clients) work.
    registry.register("read_file", "ワークスペース内のUTF-8ファイルを読む。filepathまたはpathが必要",
                      {"type": "object", "properties": {"filepath": string_param, "path": string_param}}, integrated.read_file)
    registry.register("write_file", "ワークスペース内に新規ファイルを作成。上書きしない。filepathまたはpathが必要",
                      {"type": "object", "properties": {"filepath": string_param, "path": string_param,
                       "content": string_param}, "required": ["content"]}, integrated.write_file)
    registry.register("execute_command", "コマンドを実行。既定は既存allowlist。任意シェルは隔離環境の明示設定が必要",
                      {"type": "object", "properties": {"command": string_param}, "required": ["command"]}, integrated.execute_command)
    registry.register("list_dir", "ワークスペース内ディレクトリ直下を一覧する",
                      {"type": "object", "properties": {"dirpath": string_param}}, integrated.list_dir)
    registry.register("search_photos", "Photos Pickerの選択写真を取得。session_id未指定なら選択用リンクを返す。query_filterは現在ページ内ファイル名の部分一致",
                      {"type": "object", "properties": {"query_filter": string_param,
                       "session_id": string_param, "page_token": string_param}}, integrated.search_photos)
    registry.register("get_map_directions", "Google Maps Routes APIで自動車経路を計算する",
                      {"type": "object", "properties": {"origin": string_param,
                       "destination": string_param}, "required": ["origin", "destination"]}, integrated.get_map_directions)
    return registry
