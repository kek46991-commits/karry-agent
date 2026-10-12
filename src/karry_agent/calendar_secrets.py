"""Google Calendar の OAuth JSON を環境変数用に正規化・検証する。"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

CREDENTIALS_ENV = "GOOGLE_CALENDAR_CREDENTIALS_JSON"
TOKEN_ENV = "GOOGLE_CALENDAR_TOKEN_JSON"

_WRAPPING_QUOTES = ("'", '"', "`")
_TOKEN_REQUIRED_KEYS = ("refresh_token", "client_id", "client_secret")


class CalendarSecretError(ValueError):
    """値そのものを含めずに設定ミスを伝える例外。"""


def _strip_noise(raw: str) -> str:
    text = raw.replace("\ufeff", "").strip()
    if text.startswith(("GOOGLE_CALENDAR_", "export ")) and "=" in text:
        text = text.split("=", 1)[1].strip()
    return text


def _candidates(text: str) -> list[str]:
    """そのまま・外側クォート除去・エスケープ解除の順に JSON 候補を返す。"""
    result = [text]
    stripped = text
    while len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in _WRAPPING_QUOTES:
        stripped = stripped[1:-1].strip()
        result.append(stripped)
    if '\\"' in stripped:
        result.append('"' + stripped + '"')
    return result


def _loads_object(text: str, depth: int = 0) -> Any:
    last_error: json.JSONDecodeError | None = None
    for candidate in _candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = last_error or exc
            continue
        if isinstance(value, str) and depth < 3:
            return _loads_object(_strip_noise(value), depth + 1)
        return value
    assert last_error is not None
    raise last_error


def _decode_base64(text: str) -> str | None:
    try:
        decoded = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        return decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def parse_secret_json(raw: str, env_name: str) -> dict[str, Any]:
    """前後の空白・BOM・クォート・二重エンコード・base64 を許容して JSON object を返す。"""
    text = _strip_noise(raw)
    if not text:
        raise CalendarSecretError(f"{env_name} is empty")
    if not text.lstrip("'\"`").startswith(("{", "\\")):
        decoded = _decode_base64(text.strip("'\"`"))
        if decoded is not None:
            text = _strip_noise(decoded)
    try:
        value = _loads_object(text)
    except json.JSONDecodeError as exc:
        raise CalendarSecretError(
            f"{env_name} must contain valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None
    if not isinstance(value, dict):
        raise CalendarSecretError(f"{env_name} must be a JSON object")
    return value


def validate_credentials(data: dict[str, Any]) -> dict[str, Any]:
    """OAuth クライアント JSON（installed / web）であることを確認する。"""
    section = data.get("installed") or data.get("web")
    if not isinstance(section, dict):
        raise CalendarSecretError(f"{CREDENTIALS_ENV} must have an 'installed' or 'web' section")
    missing = [key for key in ("client_id", "client_secret") if not section.get(key)]
    if missing:
        raise CalendarSecretError(f"{CREDENTIALS_ENV} is missing: {', '.join(missing)}")
    return data


def validate_token(data: dict[str, Any], credentials: dict[str, Any] | None = None) -> dict[str, Any]:
    """authorized_user token に refresh_token とクライアント情報が揃っていることを確認する。"""
    token = dict(data)
    if credentials:
        section = credentials.get("installed") or credentials.get("web") or {}
        for key in ("client_id", "client_secret"):
            if not token.get(key) and section.get(key):
                token[key] = section[key]
    missing = [key for key in _TOKEN_REQUIRED_KEYS if not token.get(key)]
    if missing:
        raise CalendarSecretError(f"{TOKEN_ENV} is missing: {', '.join(missing)}")
    return token


def to_env_value(data: dict[str, Any]) -> str:
    """Render の環境変数に貼る 1 行 JSON 文字列。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def calendar_secret_status(environ: dict[str, str]) -> dict[str, Any]:
    """値を含めずに Calendar 用環境変数の状態を返す。"""
    status: dict[str, Any] = {}
    credentials: dict[str, Any] | None = None
    for env_name in (CREDENTIALS_ENV, TOKEN_ENV):
        raw = environ.get(env_name)
        entry: dict[str, Any] = {"configured": bool(raw and raw.strip()), "valid": False}
        if entry["configured"]:
            try:
                parsed = parse_secret_json(raw or "", env_name)
                if env_name == CREDENTIALS_ENV:
                    credentials = validate_credentials(parsed)
                else:
                    validate_token(parsed, credentials)
                entry["valid"] = True
            except CalendarSecretError as exc:
                entry["error"] = str(exc)
        status[env_name] = entry
    return status
