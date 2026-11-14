"""Run a planned instruction: route each subtask to a handler, run independent branches in parallel,
persist every transition, stop runaway loops, and merge the results into one final output."""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..secret_masking import mask_text
from .planner import DEFAULT_BUDGET_TOKENS, DEFAULT_MAX_SUBTASKS, Planner, plan_subtasks
from .state import RunState, RunStateError, RunStore, Subtask
from .watch import AuditLog, MonitorGate, Verdict

logger = logging.getLogger(__name__)

Handler = Callable[[Subtask, dict[str, Any]], str]
Merger = Callable[[RunState], str]
FINISHED = {"completed", "failed", "stopped"}
CONTEXT_CHARS = 2000


@dataclass(frozen=True)
class RunLimits:
    max_attempts: int = 2
    max_workers: int = 3
    max_resumes: int = 5
    max_output_chars: int = 8000
    deadline_seconds: float = 1800.0
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    max_subtasks: int = DEFAULT_MAX_SUBTASKS


def default_merge(state: RunState) -> str:
    lines = ["# 最終出力"]
    if state.goal:
        lines.append(f"目的: {state.goal}")
    for task in state.subtasks:
        if task.status == "done":
            lines += ["", f"## {task.id}（{task.kind}）", task.output]
    unfinished = [t for t in state.subtasks if t.status != "done"]
    if unfinished:
        lines += ["", "## 未完了のサブタスク"]
        lines += [f"- {t.id}（{t.status}）: {t.error or t.prompt[:80]}" for t in unfinished]
    return "\n".join(lines)


class AutonomousRunner:
    def __init__(self, store: RunStore, handlers: dict[str, Handler], *, gate: MonitorGate | None = None,
                 limits: RunLimits | None = None, planner: Planner | None = None, merger: Merger | None = None):
        if "general" not in handlers:
            raise ValueError("handlers には general が必要です。")
        self.store = store
        self.handlers = handlers
        self.gate = gate or MonitorGate()
        self.limits = limits or RunLimits()
        self.planner = planner
        self.merger = merger
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._audits: dict[str, AuditLog] = {}

    # --- audit / persistence -------------------------------------------------
    def audit(self, run_id: str) -> AuditLog:
        with self._lock:
            if run_id not in self._audits:
                self._audits[run_id] = AuditLog(run_id, self.store.audit_path(run_id))
            return self._audits[run_id]

    def _record(self, state: RunState, event: str, **data: Any) -> None:
        try:
            entry = self.audit(state.id).append(event, data)
        except Exception as exc:
            logger.error("autonomy audit write failed: %s", type(exc).__name__)
            raise
        self.gate.publish(entry)

    def _save(self, state: RunState) -> None:
        """Keep inbound monitor signals that arrived while this worker held the state in memory."""
        with self._lock:
            try:
                fresh = self.store.load(state.id)
                state.control, state.control_reason = fresh.control, fresh.control_reason
            except RunStateError as exc:
                if exc.code != "not_found":
                    raise
            self.store.save(state)

    # --- public API ----------------------------------------------------------
    def plan(self, instruction: str, budget_tokens: int | None = None) -> tuple[str, list[Subtask]]:
        return plan_subtasks(instruction, budget_tokens=budget_tokens or self.limits.budget_tokens,
                             max_subtasks=self.limits.max_subtasks, planner=self.planner)

    def create(self, instruction: str, *, session_id: str | None = None, budget_tokens: int | None = None) -> RunState:
        goal, subtasks = self.plan(instruction, budget_tokens)
        state = RunState.new(mask_text(instruction), subtasks, goal=mask_text(goal), session_id=session_id)
        for task in state.subtasks:
            task.prompt = mask_text(task.prompt)
        self.store.save(state)
        self._record(state, "run_planned", goal=state.goal, subtasks=[
            {"id": t.id, "kind": t.kind, "depends_on": t.depends_on, "tokens": t.tokens, "prompt": t.prompt} for t in state.subtasks])
        return state

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def start_background(self, run_id: str) -> bool:
        with self._lock:
            if run_id in self._active:
                return False
            self._active.add(run_id)
        threading.Thread(target=self._execute_claimed, args=(run_id,), name=f"autonomy-{run_id[:8]}", daemon=True).start()
        return True

    def execute(self, run_id: str) -> RunState:
        with self._lock:
            if run_id in self._active:
                raise RunStateError("この実行はすでに進行中です。", "already_running")
            self._active.add(run_id)
        return self._execute_claimed(run_id)

    def signal(self, run_id: str, action: str, reason: str = "", actor: str = "owner") -> RunState:
        if action not in {"pause", "stop", "resume"}:
            raise RunStateError("action は pause / stop / resume のいずれかです。", "invalid_signal")
        with self._lock:
            state = self.store.load(run_id)
            if state.status in FINISHED:
                raise RunStateError("終了した実行には信号を送れません。", "finished")
            state.control = "" if action == "resume" else action
            state.control_reason = reason[:500]
            self.store.save(state)
            idle = run_id not in self._active
        self._record(state, "monitor_signal", action=action, reason=reason[:500], actor=actor)
        if idle and action == "stop":
            # No worker will see the signal, so the stop takes effect here.
            self._skip_pending(state, "見守り端末の停止指示により中止しました。")
            return self._finish(state, "stopped", state.control_reason or "見守り端末が停止を指示しました。")
        return state

    # --- execution -----------------------------------------------------------
    def _execute_claimed(self, run_id: str) -> RunState:
        try:
            return self._run(run_id)
        except Exception as exc:
            logger.exception("autonomy run crashed")
            try:
                state = self.store.load(run_id)
                state.status, state.hold_reason = "paused", f"内部エラーで中断しました（{type(exc).__name__}）。再開できます。"
                self._save(state)
                return state
            except Exception:  # noqa: BLE001
                raise exc from None
        finally:
            with self._lock:
                self._active.discard(run_id)

    def _recover(self, state: RunState) -> bool:
        """Prepare a stored run for (re)execution. Returns False when it must not run."""
        if state.status in FINISHED:
            return False
        if state.status == "running":
            # The previous worker died mid-run (session/process loss).
            state.resumes += 1
            if state.resumes > self.limits.max_resumes:
                self._finish(state, "failed", f"再開回数の上限（{self.limits.max_resumes} 回）に達しました。")
                return False
        for task in state.subtasks:
            if task.status == "running":
                if task.attempts >= self.limits.max_attempts:
                    task.status, task.error = "failed", "実行中の中断が繰り返されたため打ち切りました。"
                else:
                    task.status = "pending"
            elif task.status == "blocked":
                task.status = "pending"
        state.hold_reason = ""
        return True

    def _run(self, run_id: str) -> RunState:
        state = self.store.load(run_id)
        if not self._recover(state):
            return state
        state.status = "running"
        self._save(state)
        self._record(state, "run_started", resumes=state.resumes)
        started = time.monotonic()
        budget = len(state.subtasks) * self.limits.max_attempts
        while True:
            self._save(state)  # refresh inbound signals
            if state.control == "stop":
                self._skip_pending(state, "見守り端末の停止指示により中止しました。")
                return self._finish(state, "stopped", state.control_reason or "見守り端末が停止を指示しました。")
            if state.control == "pause":
                return self._pause(state, state.control_reason or "見守り端末が一時停止を指示しました。")
            self._skip_unreachable(state)
            ready = [t for t in state.subtasks if t.status == "pending" and self._deps_done(state, t)]
            if not ready:
                break
            if state.executions >= budget:
                for task in ready:
                    task.status, task.error = "failed", "実行回数の上限に達しました（ループ防止）。"
                continue
            if time.monotonic() - started > self.limits.deadline_seconds:
                return self._pause(state, "制限時間に達したため一時停止しました。再開できます。")
            batch = ready[: self.limits.max_workers]
            if not self._admit(state, batch):
                if state.control:
                    continue  # a signal arrived while the monitor was reviewing
                return state
            self._run_batch(state, [t for t in batch if t.status == "running"])
        return self._merge(state)

    @staticmethod
    def _skip_pending(state: RunState, reason: str) -> None:
        for task in state.subtasks:
            if task.status in {"pending", "blocked"}:
                task.status, task.error = "skipped", reason

    def _deps_done(self, state: RunState, task: Subtask) -> bool:
        return all(state.subtask(dep).status == "done" for dep in task.depends_on)

    def _skip_unreachable(self, state: RunState) -> None:
        changed = True
        while changed:
            changed = False
            for task in state.subtasks:
                if task.status == "pending" and any(state.subtask(d).status in {"failed", "skipped"} for d in task.depends_on):
                    task.status, task.error, changed = "skipped", "前提のサブタスクが完了しなかったため実行しません。", True

    def _admit(self, state: RunState, batch: list[Subtask]) -> bool:
        verdicts: list[tuple[Subtask, Verdict]] = [(task, self.gate.review(state, task)) for task in batch]
        for task, verdict in verdicts:
            self._record(state, "monitor_verdict", subtask=task.id, decision=verdict.decision, reason=verdict.reason, source=verdict.source)
        held = [(t, v) for t, v in verdicts if v.decision == "hold"]
        for task, verdict in verdicts:
            if verdict.decision == "deny":
                task.status, task.error = "failed", f"見守り端末により拒否されました: {verdict.reason}"
            elif held:
                task.status = "blocked" if verdict.decision == "hold" else "pending"
        if held:
            self._pause(state, held[0][1].reason)
            return False
        with self._lock:  # serialized with signal(): a stop/pause sent during review wins
            self._save(state)
            if state.control:
                return False
            for task, verdict in verdicts:
                if verdict.decision == "allow":
                    task.status, task.attempts, task.started_at, task.error = "running", task.attempts + 1, time.time(), ""
                    state.executions += 1
            self._save(state)  # persisted before work starts, so a crash is visible on resume
        for task in batch:
            if task.status == "running":
                self._record(state, "subtask_started", subtask=task.id, kind=task.kind, attempt=task.attempts)
        return True

    def _context(self, state: RunState, task: Subtask) -> dict[str, Any]:
        return {"run_id": state.id, "goal": state.goal, "session_id": state.session_id,
                "dependencies": {d: state.subtask(d).output[:CONTEXT_CHARS] for d in task.depends_on}}

    def _call(self, task: Subtask, context: dict[str, Any]) -> str:
        handler = self.handlers.get(task.kind) or self.handlers["general"]
        output = handler(task, context)
        text = str(getattr(output, "text", output) or "").strip()
        if not text:
            raise ValueError("サブタスクの結果が空でした。")
        return mask_text(text)[: self.limits.max_output_chars]

    def _run_batch(self, state: RunState, tasks: list[Subtask]) -> None:
        if not tasks:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {pool.submit(self._call, t, self._context(state, t)): t for t in tasks}
            for future in concurrent.futures.as_completed(futures):
                task = futures[future]
                task.finished_at = time.time()
                try:
                    task.output, task.status = future.result(), "done"
                    self._record(state, "subtask_done", subtask=task.id, chars=len(task.output),
                                 sha256=hashlib.sha256(task.output.encode()).hexdigest())
                except Exception as exc:  # noqa: BLE001 - one branch failing must not stop the others
                    task.error = mask_text(f"{type(exc).__name__}: {exc}")[:500]
                    task.status = "pending" if task.attempts < self.limits.max_attempts else "failed"
                    self._record(state, "subtask_failed", subtask=task.id, attempt=task.attempts, retry=task.status == "pending",
                                 error=task.error)
                self._save(state)

    def _merge(self, state: RunState) -> RunState:
        output = ""
        if self.merger is not None and any(t.status == "done" for t in state.subtasks):
            try:
                output = mask_text(str(self.merger(state) or "").strip())
            except Exception as exc:  # noqa: BLE001 - fall back to deterministic concatenation
                logger.warning("autonomy merger failed: %s", type(exc).__name__)
        state.final_output = output or default_merge(state)
        done = any(t.status == "done" for t in state.subtasks)
        return self._finish(state, "completed" if done else "failed", "" if done else "完了したサブタスクがありません。")

    def _pause(self, state: RunState, reason: str) -> RunState:
        state.status, state.hold_reason = "paused", reason[:500]
        self._save(state)
        self._record(state, "run_paused", reason=state.hold_reason)
        return state

    def _finish(self, state: RunState, status: str, reason: str = "") -> RunState:
        state.status, state.hold_reason = status, reason[:500]
        self._save(state)
        self._record(state, "run_finished", status=status, reason=state.hold_reason,
                     output_sha256=hashlib.sha256(state.final_output.encode()).hexdigest())
        return state
