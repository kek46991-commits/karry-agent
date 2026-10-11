import time
from types import SimpleNamespace

import pytest

from karry_agent.orchestrator import DelegatedTask, MultiAgentOrchestrator


def test_tasks_are_delegated_and_results_aggregated_in_input_order():
    def factory(specialist):
        def run(prompt):
            # 完了順は入力順と異なっても、集約結果は安定した入力順にする。
            if specialist == "slow":
                time.sleep(0.03)
            return SimpleNamespace(text=f"{specialist}: {prompt.upper()}")
        return run

    orchestrator = MultiAgentOrchestrator(factory, max_workers=2)
    result = orchestrator.run([
        DelegatedTask("research", "find facts", "slow"),
        DelegatedTask("draft", "write answer", "writer"),
    ])

    assert result.succeeded == 2
    assert result.failed == 0
    assert [item.task_id for item in result.results] == ["research", "draft"]
    assert "slow: FIND FACTS" in result.summary
    assert "writer: WRITE ANSWER" in result.summary


def test_one_subagent_failure_is_isolated_and_aggregated():
    def factory(specialist):
        if specialist == "broken":
            return lambda _: (_ for _ in ()).throw(RuntimeError("worker failed"))
        return lambda prompt: f"ok: {prompt}"

    result = MultiAgentOrchestrator(factory).run([
        DelegatedTask("good", "continue", "healthy"),
        DelegatedTask("bad", "fail", "broken"),
    ])

    assert result.succeeded == 1
    assert result.failed == 1
    assert result.results[1].error == "RuntimeError: worker failed"
    assert "[失敗] bad" in result.summary
    assert "[成功] good" in result.summary


def test_agent_objects_with_run_method_are_supported():
    class Agent:
        def run(self, prompt):
            return SimpleNamespace(text=f"done: {prompt}")

    result = MultiAgentOrchestrator(lambda _: Agent()).run([DelegatedTask("one", "task")])
    assert result.summary.endswith("done: task")


def test_empty_and_duplicate_tasks_are_rejected():
    orchestrator = MultiAgentOrchestrator(lambda _: lambda prompt: prompt)
    assert orchestrator.run([]).summary == "委譲されたタスクはありません。"
    with pytest.raises(ValueError):
        orchestrator.run([DelegatedTask("", "task")])
    with pytest.raises(ValueError):
        orchestrator.run([DelegatedTask("same", "one"), DelegatedTask("same", "two")])


def test_slow_subagent_is_reported_as_timeout():
    def factory(_):
        def run(_prompt):
            time.sleep(0.05)
            return "too late"
        return run

    result = MultiAgentOrchestrator(factory, timeout_seconds=0.01).run([DelegatedTask("slow", "task")])
    assert result.failed == 1
    assert "TimeoutError" in (result.results[0].error or "")
