from types import SimpleNamespace

from karry_agent.core import AgentCore
from karry_agent.orchestrator import DelegatedTask, MultiAgentOrchestrator
from karry_agent.tools import build_default_registry


def _response(parts, usage):
    content = SimpleNamespace(role="model", parts=parts)
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], usage_metadata=usage)


def _tool_part(name, args):
    return SimpleNamespace(text=None, function_call=SimpleNamespace(name=name, args=args))


def _text_part(text):
    return SimpleNamespace(text=text, function_call=None)


class EndToEndModels:
    def __init__(self, specialist):
        self.specialist = specialist
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=5, total_token_count=15)
        if self.calls == 1:
            return _response([_tool_part("get_current_datetime", {})], usage)
        return _response([_text_part(f"{self.specialist} が Observation を確認して完了")], usage)


def test_end_to_end_orchestration_connects_tools_trace_metrics_and_aggregation():
    clients = []

    def factory(specialist):
        models = EndToEndModels(specialist)
        client = SimpleNamespace(models=models)
        clients.append(client)
        return AgentCore(
            client=client,
            registry=build_default_registry(),
            max_steps=3,
            sleep_fn=lambda _: None,
        )

    orchestrator = MultiAgentOrchestrator(factory, max_workers=2)
    result = orchestrator.run([
        DelegatedTask("research", "日時を確認", "researcher"),
        DelegatedTask("review", "日時をレビュー", "reviewer"),
    ])

    assert result.succeeded == 2
    assert result.failed == 0
    assert "researcher が Observation を確認して完了" in result.summary
    assert "reviewer が Observation を確認して完了" in result.summary
    assert len(clients) == 2
    for client in clients:
        assert client.models.calls == 2


def test_end_to_end_preserves_failure_isolation():
    def factory(specialist):
        if specialist == "broken":
            raise RuntimeError("specialist unavailable")
        models = EndToEndModels(specialist)
        return AgentCore(client=SimpleNamespace(models=models), sleep_fn=lambda _: None)

    result = MultiAgentOrchestrator(factory).run([
        DelegatedTask("ok", "日時", "healthy"),
        DelegatedTask("failed", "日時", "broken"),
    ])

    assert result.succeeded == 1
    assert result.failed == 1
    assert "specialist unavailable" in (result.results[1].error or "")
    assert result.results[0].text
