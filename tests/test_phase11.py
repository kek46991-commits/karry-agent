import os
from types import SimpleNamespace

import pytest

from karry_agent.core import AgentCore
from karry_agent.tools import build_default_registry, run_local_script


def function_response(name, args):
    call = SimpleNamespace(name=name, args=args)
    part = SimpleNamespace(text=None, function_call=call)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def text_response(text):
    part = SimpleNamespace(text=text, function_call=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class AutonomousFakeModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return function_response("get_current_datetime", {})
        return text_response("Observation を確認し、完了しました")


def test_thought_action_observation_trace_is_recorded():
    models = AutonomousFakeModels()
    agent = AgentCore(client=SimpleNamespace(models=models), sleep_fn=lambda _: None)
    result = agent.run("現在時刻を確認して")

    assert result.text == "Observation を確認し、完了しました"
    assert result.tool_calls == 1
    assert len(result.trace) == 1
    event = result.trace[0]
    assert event.step == 1
    assert event.action == "get_current_datetime"
    assert isinstance(event.observation, str)
    assert event.duration_ms >= 0


def test_local_script_runs_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    script = tmp_path / "hello.py"
    script.write_text("print('script observation')", encoding="utf-8")
    result = run_local_script("hello.py")
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "script observation"


def test_local_script_rejects_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    result = run_local_script("../outside.py")
    assert "error" in result


def test_registry_exposes_autonomous_execution_tools():
    registry = build_default_registry()
    assert {"web_search", "run_command", "run_local_script"}.issubset(registry.tools)


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI_TESTS") != "1" or not os.getenv("GEMINI_API_KEY"),
    reason="Set RUN_LIVE_GEMINI_TESTS=1 and GEMINI_API_KEY to run the live tool smoke test",
)
def test_live_gemini_tool_smoke():
    result = AgentCore(max_steps=3, max_retries=2).run(
        "必ず get_current_datetime ツールを1回呼び出し、その結果を使って現在時刻を日本語で回答してください。"
    )
    assert result.text
    assert not result.text.startswith("[Error]")
    assert result.tool_calls >= 1
    assert result.trace[0].action == "get_current_datetime"
