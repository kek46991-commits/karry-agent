from pathlib import Path

from karry_agent.tools import add_calendar_event, build_default_registry


def test_calendar_tool_requires_confirmation_without_external_write():
    result = add_calendar_event("打ち合わせ", "2026-10-12T10:00:00+09:00")
    assert result["confirmation_required"] is True
    assert result["event_preview"]["summary"] == "打ち合わせ"
    assert result["event_preview"]["end"]["dateTime"] == "2026-10-12T11:00:00+09:00"


def test_calendar_tool_validates_timezone_and_duration():
    assert "timezone" in add_calendar_event("予定", "2026-10-12T10:00:00")["error"]
    assert "duration" in add_calendar_event("予定", "2026-10-12T10:00:00+09:00", 0)["error"]


def test_calendar_tool_is_registered_with_confirmation_parameter():
    registry = build_default_registry()
    declaration = registry.tools["add_calendar_event"].declaration
    assert "confirmed" in declaration["parameters"]["properties"]
    assert "start_time" in declaration["parameters"]["required"]


def test_google_secret_files_are_ignored():
    text = Path(".gitignore").read_text(encoding="utf-8")
    assert "credentials.json" in text
    assert "token.json" in text
