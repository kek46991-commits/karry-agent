"""Self-correction loop: failing check → Gemini fix → rewrite → re-run, inside an isolated copy.

The live checkout is never modified. Each job copies the repository into a private temporary
workspace, runs an allowlisted pytest check there, sends the redacted failure log plus the
allowlisted sources to Gemini (``GeminiRepairGenerator``), applies the validated exact edits to
the copy and re-runs the check, up to ``max_attempts`` times. Only a passing result produces a
diff (and, when configured, a draft PR via ``GitHubRepairPublisher``); nothing is merged or deployed.
Experimental: enabled with ``KARRY_SELF_CORRECTION=1`` and the API sits behind
``KARRY_EXPERIMENTAL_FEATURES``.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from .repair_pipeline import (
    DEFAULT_PATHS,
    GeminiRepairGenerator,
    GitHubRepairPublisher,
    source_contains_secret,
    validated_changes,
)
from .secret_masking import mask_text
from .self_healing import _SECRET_KEY, SelfHealingEngine, redact

Runner = Callable[[list[str], Path, dict[str, str], float], tuple[int, str]]
_TARGET = re.compile(r"tests/[\w./-]+\.py(?:::[\w\[\].-]+)*|tests/?")
_COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", "*.pyc",
                                      ".pytest_cache", ".ruff_cache", ".env", ".env.*")
LOG_CHARS = 8000


class SelfCorrectionError(ValueError):
    pass


def check_targets(targets: list[str] | tuple[str, ...]) -> list[str]:
    """Only pytest paths under tests/: the check command is never model- or user-defined."""
    cleaned = [str(t).strip() for t in targets if str(t).strip()]
    if not 1 <= len(cleaned) <= 10:
        raise SelfCorrectionError("tests は1〜10件で指定してください。")
    for target in cleaned:
        if not _TARGET.fullmatch(target) or ".." in PurePosixPath(target.split("::")[0]).parts:
            raise SelfCorrectionError("tests は tests/ 配下の pytest パスだけ指定できます。")
    return cleaned


def sandbox_env(workspace: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_KEY.search(k)}
    env.update(PYTHONPATH=os.pathsep.join([str(workspace / "src"), str(workspace)]), PYTHONDONTWRITEBYTECODE="1")
    return env


def run_subprocess(command: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return 124, out + f"\n[timeout after {timeout:.0f}s]"


def failure_log(output: str) -> str:
    return mask_text(str(redact(output[-LOG_CHARS:] if len(output) > LOG_CHARS else output)))


class SelfCorrectionLoop:
    def __init__(self, root: Path, *, generator: Callable[[dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
                 runner: Runner | None = None, publisher: GitHubRepairPublisher | None = None,
                 engine: SelfHealingEngine | None = None, allowed_paths: tuple[str, ...] = DEFAULT_PATHS,
                 max_attempts: int = 3, timeout: float = 300, max_jobs: int = 3,
                 default_tests: tuple[str, ...] = ("tests",)) -> None:
        self.root = root.resolve()
        self.generator = generator or GeminiRepairGenerator()
        self.runner = runner or run_subprocess
        self.publisher, self.engine = publisher, engine
        self.allowed_paths = allowed_paths
        self.max_attempts = max(1, min(max_attempts, 5))
        self.timeout = timeout
        self.max_jobs = max(1, min(max_jobs, 20))
        self.default_tests = tuple(check_targets(default_tests))
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="self-correction")

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else {k: v for k, v in job.items() if k != "_changes"}

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._jobs)
        return [self.job(i) for i in reversed(ids)]

    def submit(self, tests: list[str] | tuple[str, ...] | None = None, incident: dict[str, Any] | None = None) -> dict[str, Any]:
        targets = check_targets(list(tests or self.default_tests))
        with self._lock:
            if len(self._jobs) >= self.max_jobs:
                raise SelfCorrectionError("このプロセスで実行できる自己修正の上限に達しました。")
            job = {"job_id": uuid.uuid4().hex, "status": "queued", "tests": targets, "attempts": [],
                   "incident_id": (incident or {}).get("incident_id"), "created_at": time.time(),
                   "verified": False, "deployed": False}
            self._jobs[job["job_id"]] = job
        self._pool.submit(self._safe_run, job["job_id"], incident)
        return self.job(job["job_id"])

    def submit_incident(self, incident: dict[str, Any]) -> dict[str, Any]:
        """SelfHealingEngine patch hook for unknown code errors."""
        try:
            job = self.submit(None, incident)
        except SelfCorrectionError:
            return {"status": "diagnosed", "action": "self_correction_budget_exhausted", "verified": False}
        return {"status": "self_correction_queued", "job_id": job["job_id"], "verified": False}

    def _update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(fields, updated_at=time.time())

    def _safe_run(self, job_id: str, incident: dict[str, Any] | None) -> None:
        try:
            self._run(job_id, incident)
        except Exception as exc:  # noqa: BLE001 - a broken job must be reported, never crash the worker
            self._update(job_id, status="error", error_type=type(exc).__name__)
        job = self.job(job_id)
        if self.engine and job and job.get("incident_id"):
            self.engine.update(job["incident_id"], {k: job.get(k) for k in
                               ("status", "job_id", "verified", "deployed", "patch_sha256", "pr_url")})

    def _sources(self, workspace: Path) -> dict[str, str]:
        sources = {}
        for path in self.allowed_paths:
            target = (workspace / path).resolve()
            if workspace in target.parents and target.is_file():
                text = target.read_text(encoding="utf-8")
                if len(text) <= 50_000 and not source_contains_secret(text):
                    sources[path] = text
        return sources

    def _run(self, job_id: str, incident: dict[str, Any] | None) -> None:
        tests = self.job(job_id)["tests"]
        command = [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *tests]
        with tempfile.TemporaryDirectory(prefix="karry-self-correction-") as tmp:
            workspace = Path(tmp) / "repo"
            shutil.copytree(self.root, workspace, ignore=_COPY_IGNORE, symlinks=True)
            workspace = workspace.resolve()
            env = sandbox_env(workspace)
            originals = self._sources(workspace)
            if not originals:
                raise SelfCorrectionError("修正対象のソースを読み込めませんでした。")
            attempts: list[dict[str, Any]] = []
            self._update(job_id, status="running")
            code, output = self.runner(command, workspace, env, self.timeout)
            if code == 0:
                self._update(job_id, status="passed_without_changes", verified=True)
                return
            for number in range(1, self.max_attempts + 1):
                log = failure_log(output)
                record: dict[str, Any] = {"attempt": number, "exit_code": code, "log_tail": log[-1500:]}
                sources = self._sources(workspace)
                request = {"incident_id": (incident or {}).get("incident_id") or job_id, "error_type": "CheckFailed",
                           "check": tests, "log": log, "attempt": number,
                           "runtime_incident": redact(incident) if incident else None,
                           "previous_attempts": [{"attempt": a["attempt"], "summary": a.get("summary")} for a in attempts]}
                try:
                    proposal = self.generator(request, sources)
                    changes = validated_changes(workspace, proposal, tuple(sources))
                except Exception as exc:  # noqa: BLE001 - invalid model output is a failed attempt, not a crash
                    record["result"] = "proposal_rejected"
                    record["error_type"] = type(exc).__name__
                    attempts.append(record)
                    self._update(job_id, attempts=list(attempts))
                    continue
                for path, updated in changes.items():
                    (workspace / path).write_text(updated, encoding="utf-8")
                record.update(summary=str(proposal.get("summary") or "")[:500], files=sorted(changes))
                code, output = self.runner(command, workspace, env, self.timeout)
                record["result"] = "passed" if code == 0 else "still_failing"
                attempts.append(record)
                self._update(job_id, attempts=list(attempts))
                if code == 0:
                    self._finish(job_id, workspace, originals)
                    return
            self._update(job_id, status="not_corrected", final_log_tail=failure_log(output)[-1500:])

    def _finish(self, job_id: str, workspace: Path, originals: dict[str, str]) -> None:
        changed = {p: (workspace / p).read_text(encoding="utf-8") for p in originals
                   if (workspace / p).read_text(encoding="utf-8") != originals[p]}
        diff = "".join("".join(difflib.unified_diff(originals[p].splitlines(True), new.splitlines(True),
                                                    fromfile="a/" + p, tofile="b/" + p)) for p, new in changed.items())
        result: dict[str, Any] = {"status": "corrected", "verified": True, "validation": "tests_passed_in_sandbox",
                                  "diff": diff, "patch_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                                  "files": sorted(changed)}
        if self.publisher and changed:
            published = self.publisher.publish({"incident_id": job_id}, changed, {p: originals[p] for p in changed})
            result["pr_url"] = published.get("pr_url")
        self._update(job_id, **result)

    def close(self) -> None:
        self._pool.shutdown(wait=True)


def configure_self_correction(engine: SelfHealingEngine, root: Path) -> SelfCorrectionLoop | None:
    if os.getenv("KARRY_SELF_CORRECTION") != "1":
        return None
    publisher = None
    if os.getenv("KARRY_SELF_HEALING_CREATE_PRS") == "1":
        publisher = GitHubRepairPublisher(os.getenv("KARRY_REPAIR_GITHUB_REPO", ""), os.getenv("KARRY_REPAIR_GITHUB_TOKEN", ""),
                                          base=os.getenv("KARRY_REPAIR_GITHUB_BASE", "main"))
    tests = tuple(t for t in os.getenv("KARRY_SELF_CORRECTION_TESTS", "tests").split(",") if t.strip())
    loop = SelfCorrectionLoop(root, publisher=publisher, engine=engine, default_tests=tests,
                              max_attempts=int(os.getenv("KARRY_SELF_CORRECTION_MAX_ATTEMPTS", "3") or 3))
    engine.set_patch_hook(loop.submit_incident)
    return loop
