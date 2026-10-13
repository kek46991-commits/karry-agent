from pathlib import Path

import pytest

from karry_agent import tools
from karry_agent.tools import ToolRegistry, _safe_path, build_default_registry, get_weather


def test_write_then_read(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    registry = build_default_registry()
    created = registry.execute("write_file", {"path": "hello.txt", "content": "こんにちは"})
    assert created["bytes"] > 0
    assert registry.execute("read_file", {"path": "hello.txt"})["content"] == "こんにちは"
    assert "error" in registry.execute("write_file", {"path": "hello.txt", "content": "上書き"})


def test_path_traversal_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    with pytest.raises(PermissionError):
        _safe_path("../secret.txt")


def test_unknown_tool_returns_error():
    assert "error" in ToolRegistry().execute("missing", {})


def test_weather_tool_uses_geocoding_and_forecast(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            import json
            return json.dumps(self.payload).encode()

    responses = iter([
        Response({"results": [{"name": "枚方市", "country": "日本", "latitude": 34.81, "longitude": 135.65}]}),
        Response({"timezone": "Asia/Tokyo", "current": {"time": "2026-10-13T18:00", "temperature_2m": 22.1, "apparent_temperature": 22.0, "relative_humidity_2m": 60, "wind_speed_10m": 8.2, "weather_code": 1}}),
    ])
    monkeypatch.setattr(tools, "urlopen", lambda request, timeout=10: next(responses))

    result = get_weather("枚方市")

    assert result["location"] == "枚方市"
    assert result["current"]["condition"] == "晴れ"
    assert result["current"]["temperature_c"] == 22.1


def test_weather_tool_is_registered():
    registry = build_default_registry()
    assert "get_weather" in registry.tools
    assert "location" in registry.tools["get_weather"].declaration["parameters"]["required"]
