"""複数の専門サブエージェントを調整する軽量オーケストレーター。"""
from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass(frozen=True)
class DelegatedTask:
    """サブエージェントへ委譲する 1 つのタスク。"""

    task_id: str
    prompt: str
    specialist: str = "general"


@dataclass
class SubAgentResult:
    """委譲タスクの成功・失敗を統一した結果。"""

    task_id: str
    specialist: str
    text: str
    success: bool
    elapsed_ms: float
    error: str | None = None


@dataclass
class OrchestrationResult:
    """全サブタスクの結果と集約済みテキスト。"""

    summary: str
    results: list[SubAgentResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(result.success for result in self.results)

    @property
    def failed(self) -> int:
        return len(self.results) - self.succeeded


class MultiAgentOrchestrator:
    """サブエージェントへの委譲、並列実行、結果集約を管理する。"""

    def __init__(
        self,
        agent_factory: Callable[[str], Any],
        max_workers: int = 4,
        timeout_seconds: float = 60.0,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers は 1 以上で指定してください")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds は 0 より大きくしてください")
        self.agent_factory = agent_factory
        self.max_workers = max_workers
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _extract_text(value: Any) -> str:
        text = getattr(value, "text", value)
        return str(text).strip()

    def _run_one(self, task: DelegatedTask) -> SubAgentResult:
        started = time.perf_counter()
        try:
            agent = self.agent_factory(task.specialist)
            response = agent.run(task.prompt) if hasattr(agent, "run") else agent(task.prompt)
            text = self._extract_text(response)
            if not text:
                raise ValueError("サブエージェントから空の結果が返されました")
            return SubAgentResult(
                task.task_id,
                task.specialist,
                text,
                True,
                (time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            return SubAgentResult(
                task.task_id,
                task.specialist,
                "",
                False,
                (time.perf_counter() - started) * 1000,
                f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _aggregate(results: list[SubAgentResult]) -> str:
        if not results:
            return "委譲されたタスクはありません。"
        lines = ["サブエージェント結果:"]
        for result in results:
            status = "成功" if result.success else "失敗"
            detail = result.text if result.success else (result.error or "不明なエラー")
            lines.append(f"- [{status}] {result.task_id} ({result.specialist}): {detail}")
        return "\n".join(lines)

    def run(self, tasks: Iterable[DelegatedTask]) -> OrchestrationResult:
        task_list = list(tasks)
        if any(not task.task_id.strip() or not task.prompt.strip() for task in task_list):
            raise ValueError("task_id と prompt は空にできません")
        if len({task.task_id for task in task_list}) != len(task_list):
            raise ValueError("task_id は一意である必要があります")
        if not task_list:
            return OrchestrationResult(self._aggregate([]), [])

        results: dict[str, SubAgentResult] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            pending = {executor.submit(self._run_one, task): task for task in task_list}
            try:
                for future in concurrent.futures.as_completed(pending, timeout=self.timeout_seconds):
                    task = pending[future]
                    try:
                        results[task.task_id] = future.result()
                    except Exception as exc:
                        results[task.task_id] = SubAgentResult(
                            task.task_id,
                            task.specialist,
                            "",
                            False,
                            0.0,
                            f"{type(exc).__name__}: {exc}",
                        )
            except concurrent.futures.TimeoutError:
                for future, task in pending.items():
                    if task.task_id not in results:
                        future.cancel()
                        results[task.task_id] = SubAgentResult(
                            task.task_id,
                            task.specialist,
                            "",
                            False,
                            self.timeout_seconds * 1000,
                            "TimeoutError: サブエージェントの制限時間を超過しました",
                        )

        ordered = [results[task.task_id] for task in task_list]
        return OrchestrationResult(self._aggregate(ordered), ordered)
