from types import SimpleNamespace

import pytest

from karry_agent.core import AgentCore
from karry_agent.tools import ToolRegistry


def text_response(text="ok", usage=None):
    part = SimpleNamespace(text=text, function_call=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], usage_metadata=usage)


class UsageModels:
    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("temporary timeout")
        return text_response(
            "完了",
            SimpleNamespace(prompt_token_count=11, candidates_token_count=7, total_token_count=18),
        )


def test_metrics_record_api_calls_tokens_and_elapsed_time():
    models = UsageModels()
    result = AgentCore(client=SimpleNamespace(models=models)).run("確認")

    assert result.text == "完了"
    assert result.metrics.api_calls == 1
    assert result.metrics.input_tokens == 11
    assert result.metrics.output_tokens == 7
    assert result.metrics.total_tokens == 18
    assert result.metrics.errors == 0
    assert result.metrics.elapsed_ms >= 0


def test_metrics_record_retries_and_api_errors():
    sleeps = []
    models = UsageModels(failures=2)
    result = AgentCore(
        client=SimpleNamespace(models=models),
        max_retries=3,
        sleep_fn=sleeps.append,
    ).run("再試行")

    assert result.text == "完了"
    assert result.metrics.api_calls == 3
    assert result.metrics.retries == 2
    assert result.metrics.errors == 2
    assert sleeps == [1, 2]


def test_exhausted_api_error_returns_safe_fallback_and_metrics():
    models = UsageModels(failures=5)
    result = AgentCore(client=SimpleNamespace(models=models), max_retries=2, sleep_fn=lambda _: None).run("失敗")

    assert result.text.startswith("[Error] Gemini API/Timeout Error:")
    assert result.metrics.api_calls == 2
    assert result.metrics.retries == 1
    assert result.metrics.errors == 2


def test_unknown_tool_is_observed_as_structured_error():
    registry = ToolRegistry()
    observation = registry.execute("missing_tool", {})
    assert observation == {"error": "unknown tool: missing_tool"}


def test_empty_prompt_is_rejected_without_api_call():
    models = UsageModels()
    agent = AgentCore(client=SimpleNamespace(models=models))
    with pytest.raises(ValueError, match="空"):
        agent.run("   ")
    assert models.calls == 0
