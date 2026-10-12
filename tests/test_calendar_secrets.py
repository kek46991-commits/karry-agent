import base64
import json

import pytest
from fastapi.testclient import TestClient

from karry_agent import tools
from karry_agent.calendar_secrets import (
    CREDENTIALS_ENV,
    TOKEN_ENV,
    CalendarSecretError,
    calendar_secret_status,
    parse_secret_json,
    to_env_value,
    validate_credentials,
    validate_token,
)

CREDENTIALS = {"installed": {"client_id": "cid", "client_secret": "csecret", "token_uri": "https://oauth2.googleapis.com/token"}}
TOKEN = {"refresh_token": "rtoken", "client_id": "cid", "client_secret": "csecret", "token_uri": "https://oauth2.googleapis.com/token"}


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(TOKEN, indent=2),
        "\ufeff" + json.dumps(TOKEN) + "\n",
        "'" + json.dumps(TOKEN) + "'",
        '"' + json.dumps(TOKEN) + '"',
        json.dumps(json.dumps(TOKEN)),
        "GOOGLE_CALENDAR_TOKEN_JSON=" + json.dumps(TOKEN),
        base64.b64encode(json.dumps(TOKEN).encode()).decode(),
    ],
)
def test_parse_secret_json_tolerates_common_paste_mistakes(raw):
    assert parse_secret_json(raw, TOKEN_ENV) == TOKEN


def test_parse_secret_json_reports_position_without_value():
    with pytest.raises(CalendarSecretError) as exc:
        parse_secret_json('{"refresh_token": "rtoken",}', TOKEN_ENV)
    assert "line 1" in str(exc.value)
    assert "rtoken" not in str(exc.value)


def test_validate_credentials_requires_client_section():
    assert validate_credentials(CREDENTIALS) == CREDENTIALS
    with pytest.raises(CalendarSecretError):
        validate_credentials({"type": "service_account"})


def test_validate_token_fills_client_info_from_credentials():
    token = validate_token({"refresh_token": "rtoken"}, CREDENTIALS)
    assert token["client_id"] == "cid"
    assert token["client_secret"] == "csecret"
    with pytest.raises(CalendarSecretError, match="refresh_token"):
        validate_token({"client_id": "cid", "client_secret": "csecret"})


def test_status_never_contains_secret_values():
    status = calendar_secret_status({CREDENTIALS_ENV: json.dumps(CREDENTIALS), TOKEN_ENV: "{bad"})
    assert status[CREDENTIALS_ENV] == {"configured": True, "valid": True}
    assert status[TOKEN_ENV]["valid"] is False
    assert "csecret" not in json.dumps(status)


def test_restore_writes_normalized_json_once_per_process(tmp_path, monkeypatch):
    target = tmp_path / "token.json"
    target.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(tools, "_RESTORED_CALENDAR_SECRETS", set())
    monkeypatch.setenv(CREDENTIALS_ENV, json.dumps(CREDENTIALS))
    monkeypatch.setenv(TOKEN_ENV, "'" + json.dumps({"refresh_token": "rtoken"}) + "'")
    tools._restore_calendar_secret(TOKEN_ENV, target)
    assert json.loads(target.read_text(encoding="utf-8"))["client_id"] == "cid"
    target.write_text("refreshed", encoding="utf-8")
    tools._restore_calendar_secret(TOKEN_ENV, target)
    assert target.read_text(encoding="utf-8") == "refreshed"


def test_restore_raises_readable_error_for_invalid_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_RESTORED_CALENDAR_SECRETS", set())
    monkeypatch.setenv(CREDENTIALS_ENV, '{"installed": {}}')
    with pytest.raises(RuntimeError, match=CREDENTIALS_ENV):
        tools._restore_calendar_secret(CREDENTIALS_ENV, tmp_path / "credentials.json")


def test_calendar_health_endpoint(monkeypatch):
    from app import app

    monkeypatch.setenv(CREDENTIALS_ENV, to_env_value(CREDENTIALS))
    monkeypatch.setenv(TOKEN_ENV, to_env_value(TOKEN))
    body = TestClient(app).get("/health/calendar").json()
    assert body["ready"] is True
    assert "rtoken" not in json.dumps(body)


def test_push_script_normalizes_local_files(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("push_calendar_env", Path("scripts/push_calendar_env.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    credentials = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    credentials.write_text(json.dumps(CREDENTIALS, indent=2), encoding="utf-8")
    token.write_text("\ufeff" + json.dumps({"refresh_token": "rtoken"}), encoding="utf-8")
    assert module.main(["--credentials", str(credentials), "--token", str(token)]) == 0
    output = capsys.readouterr().out
    assert "OK" in output
    assert "rtoken" not in output
