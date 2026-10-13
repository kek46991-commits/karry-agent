from pathlib import Path
from types import SimpleNamespace

import app
from karry_agent import tools
from karry_agent.core import AgentCore, AgentResult, ExecutionEvent
from karry_agent.tools import ToolRegistry, _parse_calendar_start_time, add_calendar_event, build_default_registry, list_calendar_events, update_calendar_event


def test_calendar_tool_requires_confirmation_without_external_write():
    result = add_calendar_event("打ち合わせ", "2026-10-12T10:00:00+09:00")
    assert result["confirmation_required"] is True
    assert result["event_preview"]["summary"] == "打ち合わせ"
    assert result["event_preview"]["end"]["dateTime"] == "2026-10-12T11:00:00+09:00"


def test_calendar_tool_validates_timezone_and_duration():
    assert "hour" in add_calendar_event("予定", "明日")["error"]
    assert "duration" in add_calendar_event("予定", "2026-10-12T10:00:00+09:00", 0)["error"]


def test_relative_calendar_time_is_converted_to_jst(monkeypatch):
    class FrozenDateTime(__import__("datetime").datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 9, 30, tzinfo=tz)

    monkeypatch.setattr(tools.datetime, "datetime", FrozenDateTime)
    parsed = _parse_calendar_start_time("明日の10:00")
    assert parsed.isoformat() == "2026-10-14T10:00:00+09:00"


def test_calendar_tool_confirmed_insert_uses_primary_calendar_and_timezone(monkeypatch):
    service = _FakeService({}, {"id": "event-1", "htmlLink": "https://calendar.google.com/event"})
    monkeypatch.setattr(tools, "_calendar_service", lambda: service)

    result = add_calendar_event("予定", "2026-10-12T10:00:00+09:00", 60, confirmed=True)

    assert result["created"] is True
    assert result["event_id"] == "event-1"
    assert service.events_api.insert_request.kwargs["calendarId"] == "primary"
    assert service.events_api.insert_request.kwargs["body"]["start"] == {
        "dateTime": "2026-10-12T10:00:00+09:00",
        "timeZone": "Asia/Tokyo",
    }


def test_calendar_insert_exception_returns_safe_error(monkeypatch):
    class BrokenEvents:
        def insert(self, **kwargs):
            raise RuntimeError("auth failure")

    class BrokenService:
        def events(self):
            return BrokenEvents()

    monkeypatch.setattr(tools, "_calendar_service", lambda: BrokenService())
    result = add_calendar_event("予定", "明日の10:00", confirmed=True)
    assert result["created"] is False
    assert "認証が必要" in result["error"]
    assert "auth failure" not in result["error"]


def test_calendar_tool_is_registered_with_confirmation_parameter():
    registry = build_default_registry()
    declaration = registry.tools["add_calendar_event"].declaration
    assert "confirmed" in declaration["parameters"]["properties"]
    assert "start_time" in declaration["parameters"]["required"]


def test_google_secret_files_are_ignored():
    text = Path(".gitignore").read_text(encoding="utf-8")
    assert "credentials.json" in text
    assert "token.json" in text


class _FakeRequest:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def list(self, **kwargs):
        self.kwargs = kwargs
        return self

    def get(self, **kwargs):
        self.kwargs = kwargs
        return self

    def patch(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        return self.result


class _FakeEvents:
    def __init__(self, list_result, event_result):
        self.list_request = _FakeRequest(list_result)
        self.get_request = _FakeRequest(event_result)
        self.patch_request = _FakeRequest(event_result)
        self.insert_request = _FakeRequest(event_result)

    def list(self, **kwargs):
        self.list_request.kwargs = kwargs
        return self.list_request

    def get(self, **kwargs):
        self.get_request.kwargs = kwargs
        return self.get_request

    def patch(self, **kwargs):
        self.patch_request.kwargs = kwargs
        return self.patch_request

    def insert(self, **kwargs):
        self.insert_request.kwargs = kwargs
        return self.insert_request


class _FakeService:
    def __init__(self, list_result, event_result):
        self.events_api = _FakeEvents(list_result, event_result)

    def events(self):
        return self.events_api


def test_list_calendar_events_returns_safe_event_summaries(monkeypatch):
    service = _FakeService(
        {"items": [{"id": "event-1", "summary": "会議", "start": {"dateTime": "2026-10-12T10:00:00+09:00"}, "end": {"dateTime": "2026-10-12T11:00:00+09:00", "timeZone": "Asia/Tokyo"}, "htmlLink": "https://calendar.google.com/event"}]},
        {},
    )
    monkeypatch.setattr(tools, "_calendar_service", lambda: service)

    result = list_calendar_events("2026-10-12T00:00:00+09:00", max_results=5)

    assert result["count"] == 1
    assert result["events"][0]["event_id"] == "event-1"
    assert service.events_api.list_request.kwargs["calendarId"] == "primary"
    assert service.events_api.list_request.kwargs["timeMin"] == "2026-10-12T00:00:00+09:00"


def test_update_calendar_event_previews_then_patches_only_when_confirmed(monkeypatch):
    current = {
        "id": "event-1",
        "summary": "会議",
        "start": {"dateTime": "2026-10-12T10:00:00+09:00"},
        "end": {"dateTime": "2026-10-12T11:00:00+09:00"},
    }
    updated = {**current, "summary": "重要な会議"}
    service = _FakeService({}, current)
    service.events_api.patch_request.result = updated
    monkeypatch.setattr(tools, "_calendar_service", lambda: service)

    preview = update_calendar_event("event-1", summary="重要な会議")
    assert preview["confirmation_required"] is True
    assert preview["event_preview"]["changes"]["summary"] == "重要な会議"
    assert service.events_api.patch_request.kwargs is None

    confirmed = update_calendar_event("event-1", summary="重要な会議", confirmed=True)
    assert confirmed["updated"] is True
    assert service.events_api.patch_request.kwargs["eventId"] == "event-1"
    assert service.events_api.patch_request.kwargs["body"] == {"summary": "重要な会議"}


def test_calendar_read_and_update_tools_are_registered():
    registry = build_default_registry()
    assert "list_calendar_events" in registry.tools
    assert "update_calendar_event" in registry.tools
    assert "confirmed" in registry.tools["update_calendar_event"].declaration["parameters"]["properties"]


def test_agent_core_dispatches_function_call_to_calendar_tool():
    registry = ToolRegistry()
    observed = []
    registry.register(
        "list_calendar_events",
        "予定を取得する",
        {"type": "object", "properties": {"max_results": {"type": "integer"}}},
        lambda max_results=10: observed.append(max_results) or {"events": [], "count": 0},
    )
    function_call = SimpleNamespace(name="list_calendar_events", args={"max_results": 3})
    first = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=function_call)]))]
    )
    second = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text="予定を取得しました")]))]
    )

    class _FakeModels:
        def __init__(self):
            self.responses = [first, second]

        def generate_content(self, **kwargs):
            return self.responses.pop(0)

    fake_client = SimpleNamespace(models=_FakeModels())
    result = AgentCore(client=fake_client, model_id="test-model", registry=registry, max_steps=2).run("今日の予定を確認")

    assert result.text == "予定を取得しました"
    assert observed == [3]
    assert result.tool_calls == 1
    assert result.trace[0].action == "list_calendar_events"


def test_chat_confirmation_executes_pending_calendar_event(monkeypatch):
    session_id = "calendar-confirmation-test"
    app._PENDING_CALENDAR.clear()
    preview = AgentResult(
        "登録内容を確認してください",
        1,
        1,
        trace=[
            ExecutionEvent(
                1,
                "add_calendar_event",
                {"summary": "面談", "start_time": "2026-10-15T10:00:00+09:00", "duration_minutes": 60, "confirmed": False},
                {"confirmation_required": True},
                1.0,
            )
        ],
    )
    app._remember_calendar_preview(session_id, preview)
    calls = {}

    def fake_add(**kwargs):
        calls.update(kwargs)
        return {"created": True, "event_id": "event-1"}

    monkeypatch.setattr(app, "add_calendar_event", fake_add)
    result = app._pending_calendar_result(session_id, "はい、登録してください")

    assert result is not None
    assert result.text == "Google Calendarに登録しました。イベントID: event-1"
    assert calls == {"summary": "面談", "start_time": "2026-10-15T10:00:00+09:00", "duration_minutes": 60, "confirmed": True}
    assert session_id not in app._PENDING_CALENDAR


def test_chat_confirmation_converts_calendar_exception_to_result(monkeypatch):
    session_id = "calendar-confirmation-error-test"
    app._PENDING_CALENDAR.clear()
    app._PENDING_CALENDAR[session_id] = {
        "summary": "面談",
        "start_time": "2026-10-15T10:00:00+09:00",
        "duration_minutes": 60,
    }
    monkeypatch.setattr(app, "add_calendar_event", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("calendar unavailable")))

    result = app._pending_calendar_result(session_id, "登録して")

    assert result is not None
    assert "登録に失敗しました" in result.text
    assert "agent execution failed" not in result.text
    assert result.metrics.errors == 1
