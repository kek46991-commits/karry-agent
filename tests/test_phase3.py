from types import SimpleNamespace

from karry_agent import core
from karry_agent.core import AgentCore, run_agent
from karry_agent.tools import ToolRegistry


def _text_response(text="ok"):
    part = SimpleNamespace(text=text, function_call=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


class FlakyModels:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("temporary timeout")
        return _text_response("recovered")


class FakeClient:
    def __init__(self, failures):
        self.models = FlakyModels(failures)


def test_timeout_retries_with_exponential_backoff():
    sleeps = []
    agent = AgentCore(client=FakeClient(2), sleep_fn=sleeps.append, max_retries=3)
    result = agent.run("test")
    assert result.text == "recovered"
    assert agent.client.models.calls == 3
    assert sleeps == [1, 2]


def test_timeout_exhaustion_returns_error_without_raising():
    sleeps = []
    agent = AgentCore(client=FakeClient(5), sleep_fn=sleeps.append, max_retries=3)
    result = agent.run("test")
    assert result.text.startswith("[Error] Gemini API/Timeout Error:")
    assert agent.client.models.calls == 3
    assert sleeps == [1, 2]


def test_unexpected_error_is_not_retried():
    class BrokenModels:
        calls = 0

        def generate_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("bad configuration")

    client = SimpleNamespace(models=BrokenModels())
    agent = AgentCore(client=client, sleep_fn=lambda _: (_ for _ in ()).throw(AssertionError("should not sleep")))
    result = agent.run("test")
    assert result.text == "[Error] Unexpected Error: bad configuration"
    assert client.models.calls == 1


def test_run_agent_without_api_key_returns_string(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = run_agent("Test prompt")
    assert isinstance(result, str)
    assert result.startswith("[Error]")


def test_core_handles_empty_prompt():
    agent = AgentCore(client=FakeClient(0))
    try:
        agent.run("  ")
    except ValueError as exc:
        assert "空" in str(exc)
    else:
        raise AssertionError("empty prompt must raise ValueError")
