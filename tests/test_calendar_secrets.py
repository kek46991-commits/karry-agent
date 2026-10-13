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


def test_restore_token_does_not_require_valid_credentials_secret(tmp_path, monkeypatch):
    target = tmp_path / "token.json"
    monkeypatch.setattr(tools, "_RESTORED_CALENDAR_SECRETS", set())
    monkeypatch.setenv(CREDENTIALS_ENV, '{"client_id":"wrong-shape"}')
    monkeypatch.setenv(TOKEN_ENV, json.dumps(TOKEN))

    tools._restore_calendar_secret(TOKEN_ENV, target)

    restored = json.loads(target.read_text(encoding="utf-8"))
    assert restored["refresh_token"] == "rtoken"
    assert restored["client_id"] == "cid"


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


def _load_push_script():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("push_calendar_env_login", Path("scripts/push_calendar_env.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_push_script_login_creates_token_via_loopback(tmp_path, monkeypatch, capsys):
    import io
    import threading
    from urllib.parse import parse_qs, urlparse
    from urllib.request import urlopen as real_urlopen

    module = _load_push_script()
    seen = {}

    def fake_browser(url):
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        seen.update(query)
        callback = f"{query['redirect_uri']}?code=authcode&state={query['state']}"
        threading.Thread(target=lambda: real_urlopen(callback, timeout=5).read(), daemon=True).start()

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=30):
        body = parse_qs(request.data.decode())
        assert body["code"] == ["authcode"]
        assert body["code_verifier"][0]
        return FakeResponse(json.dumps({"access_token": "atoken", "refresh_token": "rtoken", "expires_in": 3599}).encode())

    monkeypatch.setattr(module, "_open_browser", fake_browser)
    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps(CREDENTIALS), encoding="utf-8")

    assert module.main(["--login", "--credentials", str(credentials)]) == 0
    token = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert token["refresh_token"] == "rtoken"
    assert token["client_id"] == "cid"
    assert seen["access_type"] == "offline"
    assert seen["scope"] == "https://www.googleapis.com/auth/calendar.events"
    assert "rtoken" not in capsys.readouterr().out


def test_push_script_without_token_suggests_login(tmp_path):
    module = _load_push_script()
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps(CREDENTIALS), encoding="utf-8")
    monkeypatch_roots = [tmp_path]
    module.search_roots = lambda: monkeypatch_roots
    with pytest.raises(SystemExit, match="--login"):
        module.main(["--credentials", str(credentials)])


def test_token_from_login_loads_in_google_auth(tmp_path):
    from google.oauth2.credentials import Credentials

    token = dict(TOKEN, token="atoken", scopes=["https://www.googleapis.com/auth/calendar.events"], expiry="2000-01-01T00:00:00Z")
    path = tmp_path / "token.json"
    path.write_text(json.dumps(token), encoding="utf-8")
    creds = Credentials.from_authorized_user_file(str(path), token["scopes"])
    assert creds.refresh_token == "rtoken"
    assert creds.expired is True
