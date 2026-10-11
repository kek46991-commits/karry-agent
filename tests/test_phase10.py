import os
from types import SimpleNamespace

import pytest

from karry_agent.core import AgentCore, get_client
from karry_agent.tools import build_default_registry, run_command


def test_safe_command_tool_runs_read_only_command(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    result = run_command("python3 --version")
    assert result["returncode"] == 0
    assert "Python" in result["stdout"]


def test_command_tool_rejects_shell_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    result = run_command("pwd; cat /etc/passwd")
    assert "error" in result


def test_command_tool_rejects_destructive_command(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    result = run_command("rm -rf .")
    assert "error" in result


def test_default_registry_contains_real_execution_tools():
    registry = build_default_registry()
    assert {"read_file", "write_file", "run_command"}.issubset(registry.tools)


def test_agent_config_exposes_all_registered_tools():
    agent = AgentCore(client=SimpleNamespace(), model_id="test-model")
    config = agent._config()
    declarations = config.tools[0].function_declarations
    names = {item.name if hasattr(item, "name") else item["name"] for item in declarations}
    assert "run_command" in names
    assert "read_file" in names


def test_missing_api_key_is_explicit(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        get_client()


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI_TESTS") != "1" or not os.getenv("GEMINI_API_KEY"),
    reason="Set RUN_LIVE_GEMINI_TESTS=1 and GEMINI_API_KEY to run the live Gemini smoke test",
)
def test_live_gemini_smoke():
    result = AgentCore(max_steps=2, max_retries=2).run("短く『接続成功』と回答してください。ツールは不要です。")
    assert result.text
    assert not result.text.startswith("[Error]")
