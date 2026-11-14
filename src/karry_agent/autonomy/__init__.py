"""Autonomous runs: split an instruction into subtasks, persist progress, branch, merge, and report to a monitor.

The monitor (見守り端末 / 検閲エージェント) itself is out of scope. This package only emits audit
events and obeys the verdicts/signals that a separate monitoring device or policy file provides.
"""
from .planner import estimate_tokens, plan_subtasks
from .runner import AutonomousRunner, RunLimits
from .state import JsonRunStore, MemoryRunStore, RunState, Subtask
from .watch import AuditLog, MonitorGate, Verdict

__all__ = [
    "AuditLog", "AutonomousRunner", "JsonRunStore", "MemoryRunStore", "MonitorGate", "RunLimits",
    "RunState", "Subtask", "Verdict", "estimate_tokens", "plan_subtasks",
]
