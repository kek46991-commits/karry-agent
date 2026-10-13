import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from google.genai import types

import app as web
from karry_agent import agent_core as integrated, google_workspace as google, tools
from karry_agent.core import AgentCore, AgentResult, ExecutionEvent


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_FILE", str(tmp_path / "no-token.json"))
    monkeypatch.setenv("GOOGLE_PHOTOS_TOKEN_FILE", str(tmp_path / "no-photos-token.json"))
    for name in ("KARRY_API_TOKEN", "KARRY_ALLOW_SHELL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                 "GOOGLE_REFRESH_TOKEN", "GOOGLE_CALENDAR_TOKEN_JSON", "GOOGLE_PHOTOS_TOKEN_JSON",
                 "GOOGLE_WORKSPACE_TOKEN_JSON", "GOOGLE_CALENDAR_CREDENTIALS_JSON", "GOOGLE_MAPS_API_KEY",
                 "GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH"):
        monkeypatch.delenv(name, raising=False)


def response(data):
    obj = Mock()
    obj.json.return_value = data
    obj.raise_for_status.return_value = None
    return obj


def model_response(parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=parts))])


def call(name, args=None, identifier="call-1"):
    return types.Part(function_call=types.FunctionCall(name=name, args=args or {}, id=identifier), thought_signature=b"signature")


def fake_client(responses):
    return SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=responses)))


def test_supplied_tools_registered_and_importable():
    import agent_core
    names = {func.__name__ for func in agent_core.AGENT_TOOLS}
    assert len(names) == 7
    assert names.issubset(tools.build_default_registry().tools)
    assert agent_core.dispatch_tool("list_dir", {})["status"] == "success"


def test_filepath_and_existing_path_compatibility():
    assert integrated.dispatch_tool("write_file", {"filepath": "new.txt", "content": "日本語"})["status"] == "success"
    assert integrated.dispatch_tool("read_file", {"path": "new.txt"})["content"] == "日本語"
    assert integrated.dispatch_tool("read_file", {"filepath": "new.txt"})["content"] == "日本語"
    assert integrated.dispatch_tool("write_file", {"path": "new.txt", "content": "overwrite"})["status"] == "error"
    assert integrated.read_file(filepath="a", path="b")["status"] == "error"


@pytest.mark.parametrize("name,args", [("read_file", {}), ("list_dir", {"dirpath": 3}),
                                       ("write_file", {"filepath": "x", "content": False}),
                                       ("execute_command", {"unknown": "x"}), ("list_dir", []),
                                       ("missing", {})])
def test_dispatch_invalid(name, args):
    assert "error" in integrated.dispatch_tool(name, args)


def test_file_escape_and_list_hidden(tmp_path):
    assert integrated.write_file("../escape.txt", "x")["status"] == "error"
    tmp_path.joinpath(".env").write_text("secret")
    tmp_path.joinpath("token.json").write_text("secret")
    tmp_path.joinpath("file.txt").write_text("ok")
    assert integrated.list_dir()["items"] == ["file.txt"]


def test_default_command_policy():
    assert integrated.execute_command("python3 --version")["status"] == "success"
    assert integrated.execute_command("printf foo; pwd")["status"] == "error"


def test_optin_shell_needs_auth_configuration(monkeypatch):
    monkeypatch.setenv("KARRY_ALLOW_SHELL", "1")
    assert integrated.execute_command("printf foo")["status"] == "error"


def test_optin_shell_success_failure_and_secret_env(monkeypatch):
    monkeypatch.setenv("KARRY_ALLOW_SHELL", "1")
    monkeypatch.setenv("KARRY_API_TOKEN", "test-owner-token")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sensitive")
    result = integrated.execute_command('printf "foo%s" "$GOOGLE_CLIENT_SECRET"')
    assert result["stdout"] == "foo" and result["exit_code"] == 0
    assert integrated.execute_command("exit 5")["exit_code"] == 5


def test_shell_timeout_kills_group(monkeypatch):
    monkeypatch.setenv("KARRY_ALLOW_SHELL", "1")
    monkeypatch.setenv("KARRY_API_TOKEN", "token")
    process = Mock(pid=123)
    process.communicate.side_effect = [subprocess.TimeoutExpired("x", 30), ("", "")]
    manager = Mock()
    manager.__enter__ = Mock(return_value=process)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(integrated.subprocess, "Popen", Mock(return_value=manager))
    kill = Mock()
    monkeypatch.setattr(integrated.os, "killpg", kill)
    assert integrated.execute_command("x")["error_type"] == "Command_Timeout"
    kill.assert_called_once()


def test_missing_oauth():
    with pytest.raises(ValueError, match="OAuth token"):
        google.get_google_credentials()


def test_oauth_triplet_refresh(monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "test")
    creds = Mock(valid=False)
    create = Mock(return_value=creds)
    monkeypatch.setattr(google.Credentials, "from_authorized_user_info", create)
    assert google.get_google_credentials([google.PICKER_SCOPE], photos=True) is creds
    assert create.call_args.kwargs["scopes"] == [google.PICKER_SCOPE]
    creds.refresh.assert_called_once()


def test_existing_complete_calendar_secret_ignores_bad_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_JSON", json.dumps({"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}))
    monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS_JSON", "invalid")
    creds = Mock(valid=True)
    monkeypatch.setattr(google.Credentials, "from_authorized_user_info", Mock(return_value=creds))
    assert google.get_google_credentials() is creds
    creds.refresh.assert_not_called()


def test_photos_specific_token_wins(monkeypatch):
    monkeypatch.setenv("GOOGLE_PHOTOS_TOKEN_JSON", json.dumps({"client_id": "photos", "client_secret": "secret", "refresh_token": "refresh"}))
    monkeypatch.setenv("GOOGLE_CALENDAR_TOKEN_JSON", "invalid")
    create = Mock(return_value=Mock(valid=True))
    monkeypatch.setattr(google.Credentials, "from_authorized_user_info", create)
    google.get_google_credentials([google.PICKER_SCOPE], photos=True)
    assert create.call_args.args[0]["client_id"] == "photos"


def test_oauth_refresh_error_no_secret_leak(monkeypatch):
    monkeypatch.setattr(google, "get_google_credentials", Mock(side_effect=RefreshError("private-token")))
    result = google.search_photos()
    assert result["error_type"] == "OAuth_Refresh_Error"
    assert "private-token" not in str(result)


def picker(monkeypatch):
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(google, "get_google_credentials", Mock())
    monkeypatch.setattr(google, "AuthorizedSession", Mock(return_value=session))
    return session


def test_picker_create(monkeypatch):
    session = picker(monkeypatch)
    session.post.return_value = response({"id": "sid", "pickerUri": "https://photos.google.com/picker/test"})
    result = integrated.dispatch_tool("search_photos", {})
    assert result["status"] == "needs_user_action" and result["session_id"] == "sid"


def test_picker_waiting(monkeypatch):
    session = picker(monkeypatch)
    session.get.return_value = response({"mediaItemsSet": False})
    assert google.search_photos(session_id="sid")["status"] == "needs_user_action"
    assert session.get.call_count == 1


def test_picker_selected_filter_and_pagination(monkeypatch):
    session = picker(monkeypatch)
    session.get.side_effect = [response({"mediaItemsSet": True}), response({"mediaItems": [
        {"id": "1", "mediaFile": {"filename": "Trip.jpg"}},
        {"id": "2", "mediaFile": {"filename": "work.jpg"}}], "nextPageToken": "next"})]
    result = google.search_photos("trip", "sid", "previous")
    assert [item["id"] for item in result["items"]] == ["1"]
    assert result["nextPageToken"] == "next"
    assert session.get.call_args.kwargs["params"]["pageToken"] == "previous"


def test_maps_missing_key_not_stub():
    assert integrated.get_map_directions("東京駅", "品川駅")["status"] == "error"


def test_maps_api_request(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    post = Mock(return_value=response({"routes": [{"distanceMeters": 123, "duration": "60s"}]}))
    monkeypatch.setattr(google.requests, "post", post)
    result = integrated.dispatch_tool("get_map_directions", {"origin": "東京駅", "destination": "品川駅"})
    assert result["routes"][0]["distanceMeters"] == 123
    assert post.call_args.kwargs["json"]["origin"] == {"address": "東京駅"}
    assert post.call_args.kwargs["headers"]["X-Goog-Api-Key"] == "test-key"


def test_maps_http_error_and_empty_routes(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    resp = requests.Response()
    resp.status_code = 429
    monkeypatch.setattr(google.requests, "post", Mock(side_effect=requests.HTTPError("private", response=resp)))
    assert google.get_map_directions("a", "b")["http_status"] == 429
    monkeypatch.setattr(google.requests, "post", Mock(return_value=response({})))
    assert google.get_map_directions("a", "b")["error_type"] == "Route_Not_Found"


def test_calendar_supplied_end_description_and_invalid_end():
    result = integrated.add_calendar_event("予定", "2026-10-14T10:00:00+09:00", "2026-10-14T12:00:00+09:00", "説明")
    assert result["status"] == "needs_confirmation"
    assert result["event_preview"]["description"] == "説明"
    assert result["event_preview"]["end"]["dateTime"].endswith("12:00:00+09:00")
    assert integrated.add_calendar_event("予定", "2026-10-14T10:00:00+09:00", "2026-10-14T09:00:00+09:00")["status"] == "error"


def test_calendar_confirmation_preserves_supplied_args(monkeypatch):
    args = {"summary": "予定", "start_time": "2026-10-14T10:00:00+09:00",
            "end_time": "2026-10-14T12:00:00+09:00", "description": "説明"}
    preview = AgentResult("確認", 1, 1, trace=[ExecutionEvent(1, "add_calendar_event", args,
                          {"confirmation_required": True}, 1)])
    web._remember_calendar_preview("unified-test", preview)
    create = Mock(return_value={"created": True, "event_id": "evt"})
    monkeypatch.setattr(web, "add_calendar_event", create)
    web._pending_calendar_result("unified-test", "はい")
    assert create.call_args.kwargs["end_time"] == args["end_time"]
    assert create.call_args.kwargs["description"] == "説明"


def test_gemini_id_signature_and_manual_calling():
    first = model_response([call("list_dir")])
    client = fake_client([first, model_response([types.Part.from_text(text="完了")])])
    result = AgentCore(client=client).run("一覧")
    assert result.text == "完了" and result.tool_calls == 1
    history = client.models.generate_content.call_args.kwargs["contents"]
    assert history[1] is first.candidates[0].content
    assert history[1].parts[0].thought_signature == b"signature"
    assert history[2].parts[0].function_response.id == "call-1"
    assert client.models.generate_content.call_args.kwargs["config"].automatic_function_calling.disable is True


def test_gemini_operation_wait_skips_later_calls(monkeypatch):
    handler = Mock(return_value={"status": "needs_user_action", "pickerUri": "https://photos.google.com/picker/test", "session_id": "sid", "message": "選択してください"})
    registry = tools.ToolRegistry()
    registry.register("search_photos", "pick", {}, handler)
    later = Mock(return_value={"status": "success"})
    registry.register("later", "later", {}, later)
    client = fake_client([model_response([call("search_photos"), call("later", identifier="second")])])
    result = AgentCore(client=client, registry=registry).run("写真")
    assert result.status == "needs_user_action" and result.tool_calls == 1
    assert "sid" in result.text
    later.assert_not_called()
    assert result.trace[1].observation["status"] == "not_executed"


def test_core_tool_errors_counted():
    client = fake_client([model_response([call("unknown")]), model_response([types.Part.from_text(text="失敗")])])
    assert AgentCore(client=client).run("失敗").metrics.errors == 1


def test_api_direct_tools_fail_closed_and_bind(monkeypatch):
    with TestClient(web.app) as client:
        assert client.get("/api/agent/tools").status_code == 503
        monkeypatch.setenv("KARRY_API_TOKEN", "owner-token")
        assert client.get("/api/agent/tools").status_code == 401
        headers = {"Authorization": "Bearer owner-token"}
        declarations = client.get("/api/agent/tools", headers=headers).json()["tools"]
        assert "search_photos" in {item["name"] for item in declarations}
        result = client.post("/api/agent/tools/execute", headers=headers,
                             json={"tool_name": "list_dir", "arguments": {}})
        assert result.json()["result"]["status"] == "success"


@pytest.mark.parametrize("route", ["/run", "/chat", "/agent/run", "/sessions/x", "/runs/x"])
def test_existing_routes_protected_when_configured(monkeypatch, route):
    monkeypatch.setenv("KARRY_API_TOKEN", "owner-token")
    with TestClient(web.app) as client:
        result = client.post(route, json={"prompt": "hello", "task": "hello"}) if route in {"/run", "/chat", "/agent/run"} else client.get(route)
        assert result.status_code == 401


def test_shell_without_token_closes_legacy_execution(monkeypatch):
    monkeypatch.setenv("KARRY_ALLOW_SHELL", "1")
    with TestClient(web.app) as client:
        assert client.post("/agent/run", json={"task": "test"}).status_code == 503


def test_chat_api_retains_user_action_status(monkeypatch):
    class FakeAgent:
        def run(self, prompt):
            return AgentResult("選択", 1, 1, status="needs_user_action")
    web.app.dependency_overrides[web.get_agent] = FakeAgent
    try:
        with TestClient(web.app) as client:
            result = client.post("/chat", json={"prompt": "写真", "session_id": "unified-photos"})
        assert result.json()["status"] == "needs_user_action"
    finally:
        web.app.dependency_overrides.pop(web.get_agent, None)


def test_model_cannot_self_confirm_calendar_insert():
    handler = Mock(return_value={"confirmation_required": True, "status": "needs_confirmation"})
    registry = tools.ToolRegistry()
    registry.register("add_calendar_event", "preview", {"type": "object", "properties": {}}, handler)
    client = fake_client([model_response([call("add_calendar_event", {"summary": "test", "confirmed": True})]),
                          model_response([types.Part.from_text(text="確認してください")])])
    result = AgentCore(client=client, registry=registry).run("予定")
    assert handler.call_args.kwargs["confirmed"] is False
    assert result.trace[0].arguments["confirmed"] is False


def test_run_issues_session_for_preview_confirmation():
    class FakeAgent:
        def run(self, prompt):
            return AgentResult("確認", 1, 0)
    web.app.dependency_overrides[web.get_agent] = FakeAgent
    try:
        with TestClient(web.app) as client:
            result = client.post("/run", json={"prompt": "予定"})
        assert result.json()["session_id"]
    finally:
        web.app.dependency_overrides.pop(web.get_agent, None)


def test_required_content_not_silently_empty():
    result = integrated.dispatch_tool("write_file", {"filepath": "blank.txt"})
    assert result["status"] == "error"


def test_calendar_confirm_uses_preview_absolute_time(monkeypatch):
    args = {"summary": "予定", "start_time": "明日の10:00"}
    event = {"start": {"dateTime": "2026-10-14T10:00:00+09:00"},
             "end": {"dateTime": "2026-10-14T11:00:00+09:00"}}
    preview = AgentResult("確認", 1, 1, trace=[ExecutionEvent(1, "add_calendar_event", args,
                          {"confirmation_required": True, "event_preview": event}, 1)])
    web._remember_calendar_preview("relative-test", preview)
    create = Mock(return_value={"created": True, "event_id": "evt"})
    monkeypatch.setattr(web, "add_calendar_event", create)
    web._pending_calendar_result("relative-test", "はい")
    assert create.call_args.kwargs["start_time"] == "2026-10-14T10:00:00+09:00"
    assert create.call_args.kwargs["end_time"] == "2026-10-14T11:00:00+09:00"
