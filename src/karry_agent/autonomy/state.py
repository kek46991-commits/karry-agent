"""Durable run state. Every status change is written before and after work so a crash can resume."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

RUN_ID = re.compile(r"^[a-f0-9]{32}$")
SUBTASK_STATUSES = {"pending", "running", "done", "failed", "skipped", "blocked"}
RUN_STATUSES = {"planned", "running", "paused", "completed", "failed", "stopped"}
MAX_RUNS = 200


class RunStateError(Exception):
    def __init__(self, message: str, code: str = "run_state"):
        super().__init__(message)
        self.code = code


@dataclass
class Subtask:
    id: str
    prompt: str
    kind: str = "general"
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0
    output: str = ""
    error: str = ""
    tokens: int = 0
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class RunState:
    id: str
    instruction: str
    subtasks: list[Subtask]
    status: str = "planned"
    session_id: str | None = None
    goal: str = ""
    final_output: str = ""
    executions: int = 0
    resumes: int = 0
    control: str = ""
    control_reason: str = ""
    hold_reason: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def new(cls, instruction: str, subtasks: list[Subtask], *, goal: str = "", session_id: str | None = None) -> RunState:
        return cls(uuid.uuid4().hex, instruction, subtasks, goal=goal, session_id=session_id)

    def subtask(self, subtask_id: str) -> Subtask:
        for task in self.subtasks:
            if task.id == subtask_id:
                return task
        raise RunStateError("サブタスクが見つかりません。", "not_found")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        if not isinstance(data, dict) or not RUN_ID.fullmatch(str(data.get("id", ""))):
            raise RunStateError("保存された実行状態の形式が不正です。", "storage_integrity")
        values = dict(data)
        values["subtasks"] = [Subtask(**item) for item in values.get("subtasks", [])]
        state = cls(**values)
        if state.status not in RUN_STATUSES or any(t.status not in SUBTASK_STATUSES for t in state.subtasks):
            raise RunStateError("保存された実行状態のステータスが不正です。", "storage_integrity")
        return state

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in self.subtasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return {"id": self.id, "status": self.status, "goal": self.goal, "subtasks": len(self.subtasks),
                "counts": counts, "executions": self.executions, "resumes": self.resumes,
                "control": self.control, "hold_reason": self.hold_reason,
                "created_at": self.created_at, "updated_at": self.updated_at}


class RunStore(Protocol):
    def save(self, state: RunState) -> None: ...
    def load(self, run_id: str) -> RunState: ...
    def list(self) -> list[RunState]: ...
    def audit_path(self, run_id: str) -> Path | None: ...


def _check_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise RunStateError("実行IDの形式が不正です。", "invalid_run")
    return run_id


class MemoryRunStore:
    """Process-local store for tests and KARRY_TASK_STORAGE=memory deployments."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def save(self, state: RunState) -> None:
        state.updated_at = time.time()
        with self._lock:
            if state.id not in self._runs and len(self._runs) >= MAX_RUNS:
                raise RunStateError(f"保存できる実行は {MAX_RUNS} 件までです。", "storage_capacity")
            self._runs[state.id] = copy.deepcopy(state.to_dict())

    def load(self, run_id: str) -> RunState:
        with self._lock:
            data = self._runs.get(_check_id(run_id))
        if data is None:
            raise RunStateError("実行が見つかりません。", "not_found")
        return RunState.from_dict(copy.deepcopy(data))

    def list(self) -> list[RunState]:
        with self._lock:
            items = [copy.deepcopy(value) for value in self._runs.values()]
        return sorted((RunState.from_dict(item) for item in items), key=lambda s: s.created_at, reverse=True)

    def audit_path(self, run_id: str) -> Path | None:
        return None


class JsonRunStore:
    """One JSON file per run, replaced atomically (write temp file, fsync, os.replace)."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    def _path(self, run_id: str) -> Path:
        return self.root / f"{_check_id(run_id)}.json"

    def audit_path(self, run_id: str) -> Path | None:
        return self.root / f"{_check_id(run_id)}.audit.jsonl"

    def save(self, state: RunState) -> None:
        state.updated_at = time.time()
        path = self._path(state.id)
        with self._lock:
            if not path.exists() and len(list(self.root.glob("*.json"))) >= MAX_RUNS:
                raise RunStateError(f"保存できる実行は {MAX_RUNS} 件までです。", "storage_capacity")
            temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state.to_dict(), handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if temp.exists():
                    temp.unlink()

    def load(self, run_id: str) -> RunState:
        path = self._path(run_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunStateError("実行が見つかりません。", "not_found") from exc
        except (OSError, ValueError) as exc:
            raise RunStateError("保存された実行状態を読み込めません。", "storage_integrity") from exc
        return RunState.from_dict(data)

    def list(self) -> list[RunState]:
        states = []
        for path in self.root.glob("*.json"):
            try:
                states.append(self.load(path.stem))
            except RunStateError:
                continue
        return sorted(states, key=lambda s: s.created_at, reverse=True)
