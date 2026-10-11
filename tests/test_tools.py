from pathlib import Path

import pytest

from karry_agent.tools import ToolRegistry, _safe_path, build_default_registry


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
