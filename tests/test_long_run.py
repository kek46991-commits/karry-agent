from types import SimpleNamespace

import pytest
import requests

from karry_agent.core import KEEP_RECENT_STEPS, AgentCore, state_digest
from karry_agent.light_llm import LightDecision, LightProvider
from karry_agent.tools import ToolRegistry


def part_text(text):
    return SimpleNamespace(text=text, function_call=None)


def part_call(name, args):
    return SimpleNamespace(text=None, function_call=SimpleNamespace(name=name, args=args))


def response(parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=parts))])


class ScriptedModels:
    def __init__(self, tool_steps):
        self.tool_steps = tool_steps
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append({"contents": list(kwargs["contents"]), "config": kwargs["config"]})
        if len(self.calls) <= self.tool_steps:
            return response([part_call("echo", {"value": str(len(self.calls))})])
        return response([part_text("完了")])


def client(tool_steps):
    return SimpleNamespace(models=ScriptedModels(tool_steps))


def registry():
    reg = ToolRegistry()
    reg.register("echo", "テスト", {"type": "object", "properties": {"value": {"type": "string"}}},
                 lambda value: {"status": "ok", "echo": value * 50})
    reg.register("write_file", "書き込み", {"type": "object", "properties": {}}, lambda: {"status": "ok"})
    return reg


def text_of(content):
    return "".join(getattr(p, "text", None) or "" for p in content.parts)


def test_state_compaction_keeps_context_bounded_over_long_loops(monkeypatch):
    monkeypatch.setenv("KARRY_COMPACT_EVERY", "4")
    fake = client(tool_steps=90)
    result = AgentCore(client=fake, registry=registry(), light_provider=None).run("長いタスク")
    assert result.text == "完了" and result.steps == 91 and result.tool_calls == 90
    assert len(result.trace) == 90
    assert result.metrics.compactions > 15
    max_len = 1 + 2 * (KEEP_RECENT_STEPS + 4 - 1)
    assert max(len(call["contents"]) for call in fake.models.calls) <= max_len
    last_first = text_of(fake.models.calls[-1]["contents"][0])
    assert "長いタスク" in last_first and "これまでの実行の要約" in last_first and "省略" in last_first
    assert len(last_first) < 6000


def test_compaction_can_be_disabled(monkeypatch):
    monkeypatch.setenv("KARRY_COMPACT_EVERY", "0")
    fake = client(tool_steps=10)
    result = AgentCore(client=fake, registry=registry(), light_provider=None).run("タスク")
    assert result.metrics.compactions == 0
    assert len(fake.models.calls[-1]["contents"]) == 21


def test_state_digest_is_bounded_and_newest_first_detail():
    from karry_agent.core import ExecutionEvent
    events = [ExecutionEvent(i, "echo", {"value": i}, {"status": "ok", "data": "x" * 5000}, 1.0) for i in range(1, 200)]
    digest = state_digest(events, budget=3000)
    assert len(digest) < 3500
    assert "step 199" in digest and "step 1:" not in digest and "echo×" in digest


class FakeLight:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.declarations = []

    def decide(self, system, user, declarations):
        self.declarations.append([d["name"] for d in declarations])
        return self.decisions.pop(0) if self.decisions else LightDecision(done=True)


def test_light_provider_handles_intermediate_steps_and_gemini_answers():
    fake = client(tool_steps=1)
    light = FakeLight([LightDecision(calls=[("echo", {"value": "a"})], tokens=10),
                       LightDecision(calls=[("echo", {"value": "b"})], tokens=10),
                       LightDecision(done=True, tokens=5)])
    result = AgentCore(client=fake, registry=registry(), light_provider=light).run("調べて")
    assert result.text == "完了"
    assert [e.arguments["value"] for e in result.trace] == ["1", "a", "b"]
    assert len(fake.models.calls) == 2
    assert result.metrics.light_api_calls == 3 and result.metrics.light_tokens == 25
    final = fake.models.calls[-1]
    assert final["config"].tools == []
    assert "最終回答" in text_of(final["contents"][0]) and "step 3" in text_of(final["contents"][0])
    assert all("write_file" not in names for names in light.declarations)


def test_light_failure_or_repeat_falls_back_to_gemini():
    fake = client(tool_steps=2)
    light = FakeLight([None, LightDecision(calls=[("echo", {"value": "2"})])])
    result = AgentCore(client=fake, registry=registry(), light_provider=light).run("調べて")
    assert result.text == "完了"
    assert len(fake.models.calls) == 3
    assert result.tool_calls == 2


def test_private_mode_never_uses_light_provider():
    light = FakeLight([LightDecision(calls=[("echo", {"value": "x"})])])
    result = AgentCore(client=client(tool_steps=1), registry=registry(), light_provider=light).run("秘密", private_mode=True)
    assert result.text == "完了" and light.declarations == []


class FakeSession:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.requests = payload, error, []

    def post(self, url, json, timeout, headers):
        self.requests.append({"url": url, "json": json, "timeout": timeout, "headers": headers})
        if self.error:
            raise self.error
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: self.payload)


DECLS = [{"name": "echo", "description": "d", "parameters": {"type": "object", "properties": {}}}]


def test_openai_compatible_decide_parses_tool_calls():
    session = FakeSession({"choices": [{"message": {"tool_calls": [
        {"function": {"name": "echo", "arguments": '{"value": "1"}'}}]}}], "usage": {"total_tokens": 42}})
    token = "gsk_" + "abcdefghijklmnopqrstuvwxyz0123"
    provider = LightProvider("groq", token, session=session)
    decision = provider.decide("sys", "key=" + "sk-" + "abcdefghijklmnopqrstuvwx", DECLS)
    assert decision.calls == [("echo", {"value": "1"})] and decision.tokens == 42 and not decision.done
    sent = session.requests[0]
    assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {token}" and sent["timeout"] == 30.0
    assert "abcdefghijklmnopqrstuvwx" not in sent["json"]["messages"][1]["content"]
    assert sent["json"]["tools"][0]["function"]["name"] == "echo"


@pytest.mark.parametrize("session", [
    FakeSession({"choices": [{"message": {"content": "DONE"}}]}),
    FakeSession({"choices": [{"message": {"tool_calls": [{"function": {"name": "rm_rf", "arguments": "{}"}}]}}]}),
    FakeSession(error=requests.Timeout()),
    FakeSession({"choices": []}),
])
def test_openai_compatible_decide_done_or_fail_closed(session):
    decision = LightProvider("deepseek", "k", session=session).decide("s", "u", DECLS)
    if session.payload and session.payload.get("choices") and "content" in session.payload["choices"][0]["message"]:
        assert decision.done and decision.calls == []
    else:
        assert decision is None
    assert session.requests[0]["url"] == "https://api.deepseek.com/chat/completions"


def test_light_provider_from_env(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("KARRY_LIGHT_PROVIDER", "groq")
    assert LightProvider.from_env() is None
    monkeypatch.setenv("GROQ_API_KEY", "YOUR_GROQ_API_KEY")
    assert LightProvider.from_env() is None
    monkeypatch.setenv("GROQ_API_KEY", "real")
    monkeypatch.setenv("KARRY_LIGHT_MODEL", "llama-3.1-8b-instant")
    provider = LightProvider.from_env()
    assert provider.name == "groq" and provider.model == "llama-3.1-8b-instant"
    monkeypatch.setenv("KARRY_LIGHT_PROVIDER", "unknown")
    assert LightProvider.from_env() is None
    monkeypatch.delenv("KARRY_LIGHT_PROVIDER")
    assert AgentCore(client=client(0)).light_provider is None


@pytest.mark.parametrize("raw", ["-1", "201", "x"])
def test_invalid_compact_every(raw, monkeypatch):
    monkeypatch.setenv("KARRY_COMPACT_EVERY", raw)
    with pytest.raises(ValueError, match="KARRY_COMPACT_EVERY"):
        AgentCore(client=client(0))
