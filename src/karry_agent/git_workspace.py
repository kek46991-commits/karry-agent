"""Bounded Git/GitHub operations for the agent's configured workspace.

Unlike a shell command wrapper, this API accepts an operation enum and builds
argument arrays with shell=False. It never accepts an arbitrary workspace path.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from .self_healing import redact

_TIMEOUT = 30
_MAX_TEXT = 6_000
_BRANCH_RE = re.compile(r"^(?:agent|feature|feat|fix|chore)/[a-z0-9][a-z0-9._/-]{0,99}$")
_GITHUB_HTTPS_RE = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
_PROTECTED_PATH_RE = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|\.git|\.aws|\.ssh|node_modules|\.venv|venv|__pycache__|"
    r"credentials\.json|token\.json|id_(?:rsa|ed25519)|[^/]+\.(?:pem|key|p12|pfx)|secrets?)(?:$|/)",
    re.IGNORECASE,
)
_ALLOWED_OPERATIONS = {
    "version", "status", "remotes", "branch_info", "initialize", "add_remote", "create_branch",
    "stage", "review", "commit", "push", "create_pr",
}


def _valid_branch_name(value: str | None) -> bool:
    if not value or not _BRANCH_RE.fullmatch(value) or ".." in value or "//" in value or "@{" in value:
        return False
    return all(part and not part.startswith(".") and not part.endswith(".") and not part.endswith(".lock")
               for part in value.split("/"))


def _root() -> Path:
    return Path(os.getenv("KARRY_WORKSPACE", os.getcwd())).expanduser().resolve()


def _run(program: str, args: list[str], *, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(program)
    if not executable:
        raise FileNotFoundError(f"{program} is not installed")
    return subprocess.run(
        [executable, *args], cwd=_root(), capture_output=True, text=True,
        timeout=timeout, check=False, shell=False,
    )


def _safe_text(value: str) -> str:
    text = re.sub(r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
                  "[redacted private key]", value)
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", text)
    text = redact(text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
                  "[redacted GitHub token]", text)
    text = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[redacted cloud key]", text)
    text = re.sub(r"\bAIza[A-Za-z0-9_-]{30,}\b", "[redacted API key]", text)
    return text[:_MAX_TEXT]


def _git(args: list[str], *, timeout: int = _TIMEOUT) -> subprocess.CompletedProcess[str]:
    return _run("git", ["-c", "color.ui=false", *args], timeout=timeout)


def _result(command: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "status": "success" if result.returncode == 0 else "error",
        "operation": command,
        "exit_code": result.returncode,
        "stdout": _safe_text(result.stdout),
        "stderr": _safe_text(result.stderr),
    }


def _repo_root() -> tuple[Path | None, dict[str, Any] | None]:
    root = _root()
    if not root.is_dir():
        return None, {"status": "error", "error": "configured workspace directory does not exist"}
    result = _git(["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None, {"status": "error", "error": "workspace is not a Git repository; initialization was not attempted"}
    top = Path(result.stdout.strip()).resolve()
    if top != root:
        return None, {"status": "error", "error": "workspace must be the Git repository root; parent repositories are not modified"}
    return root, None


def _current_branch() -> str | None:
    result = _git(["branch", "--show-current"])
    return result.stdout.strip() if result.returncode == 0 else None


def _is_protected(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    return not normalized or normalized in {".", ".."} or bool(_PROTECTED_PATH_RE.search(normalized))


def _validate_paths(paths: list[str] | None) -> tuple[list[str] | None, str | None]:
    if not isinstance(paths, list) or not paths or len(paths) > 50:
        return None, "paths must list 1–50 explicit workspace-relative paths"
    root = _root()
    clean: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not value or value.startswith("-") or Path(value).is_absolute():
            return None, "only explicit relative file paths are accepted"
        if any(char in value for char in ("*", "?", "[", "]", "\n", "\r")):
            return None, "globs and shell metacharacters are not accepted; list files explicitly"
        candidate = (root / value).resolve()
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            return None, "paths outside the configured workspace are not allowed"
        if _is_protected(relative):
            return None, f"sensitive/generated path cannot be staged: {relative}"
        if relative not in clean:
            clean.append(relative)
    return clean, None


def _staged_names() -> list[str] | None:
    result = _git(["diff", "--cached", "--name-only", "--no-ext-diff", "--no-color"])
    if result.returncode:
        return None
    return [line for line in result.stdout.splitlines() if line]


def _review() -> dict[str, Any]:
    names = _staged_names()
    if names is None:
        return {"status": "error", "error": "could not read staged changes"}
    protected = [name for name in names if _is_protected(name)]
    if protected:
        return {"status": "error", "error": "staged changes include protected paths; do not commit or push",
                "protected_paths": protected}
    diff_check = _git(["diff", "--cached", "--check"])
    stat = _git(["diff", "--cached", "--stat", "--no-ext-diff", "--no-color"])
    patch = _git(["diff", "--cached", "--no-ext-diff", "--no-color", "--no-renames"], timeout=10)
    return {
        "status": "success" if diff_check.returncode == 0 and stat.returncode == 0 and patch.returncode == 0 else "error",
        "staged_paths": names,
        "diff_check": _safe_text(diff_check.stdout + diff_check.stderr),
        "stat": _safe_text(stat.stdout),
        "review_diff": _safe_text(patch.stdout),
        "review_diff_truncated": len(patch.stdout) > _MAX_TEXT,
        "note": "Review the redacted diff and verify tests before commit/push.",
    }


def run_git_command(
    operation: str,
    paths: list[str] | None = None,
    message: str | None = None,
    branch_name: str | None = None,
    title: str | None = None,
    body: str | None = None,
    confirm_reviewed_diff: bool = False,
    remote_url: str | None = None,
    confirm_initialization: bool = False,
    confirm_remote_change: bool = False,
) -> dict[str, Any]:
    """Run one enumerated Git operation; never runs arbitrary shell text or installs software."""
    if operation not in _ALLOWED_OPERATIONS:
        return {"status": "error", "error": "operation is not allowed", "allowed_operations": sorted(_ALLOWED_OPERATIONS)}
    try:
        if operation == "version":
            result = _run("git", ["--version"])
            return _result(operation, result)

        if operation == "initialize":
            if not confirm_initialization:
                return {"status": "error", "error": "confirm_initialization=true is required; no repository was created"}
            root = _root()
            if not root.is_dir():
                return {"status": "error", "error": "configured workspace directory does not exist"}
            existing_repo = _git(["rev-parse", "--show-toplevel"])
            if existing_repo.returncode == 0 or (root / ".git").exists():
                return {"status": "error", "error": "workspace is already in a Git repository; initialization refused"}
            initial_branch = branch_name or f"agent/initial-import-{date.today().isoformat()}"
            if not _valid_branch_name(initial_branch) or initial_branch.lower() in {"main", "master"}:
                return {"status": "error", "error": "initial branch must use an approved feature/agent prefix, never main/master"}
            result = _git(["init", f"--initial-branch={initial_branch}", "."])
            return _result(operation, result) | ({"branch": initial_branch} if result.returncode == 0 else {})

        root, error = _repo_root()
        if error:
            if operation == "create_branch" and error.get("error", "").startswith("workspace is not"):
                return {**error, "next_step": "Confirm the target repository and remote first; initialization is not automatic."}
            return error

        if operation == "add_remote":
            if not confirm_remote_change:
                return {"status": "error", "error": "confirm_remote_change=true and an exact repository URL are required"}
            if not remote_url or len(remote_url) > 200 or not (_GITHUB_HTTPS_RE.fullmatch(remote_url) or _GITHUB_SSH_RE.fullmatch(remote_url)):
                return {"status": "error", "error": "remote_url must be a credential-free GitHub HTTPS or SSH repository URL"}
            remotes = _git(["remote"])
            if remotes.returncode or "origin" in remotes.stdout.splitlines():
                return {"status": "error", "error": "origin already exists or remotes could not be read; refusing to replace it"}
            result = _git(["remote", "add", "origin", remote_url])
            if result.returncode:
                return _result(operation, result)
            return {"status": "success", "operation": operation, "remote": "origin",
                    "repository_url": remote_url.removesuffix(".git")}

        if operation == "status":
            result = _result(operation, _git(["status", "--short", "--branch"]))
            result["workspace_path"] = str(root)
            return result
        if operation == "remotes":
            result = _git(["remote", "-v"])
            return _result(operation, result)
        if operation == "branch_info":
            result = _git(["branch", "--show-current"])
            if result.returncode:
                return _result(operation, result)
            return {"status": "success", "operation": operation, "current_branch": result.stdout.strip(),
                    "is_protected_branch": result.stdout.strip().lower() in {"main", "master"}}
        if operation == "create_branch":
            if not _valid_branch_name(branch_name) or branch_name.lower() in {"main", "master"}:
                return {"status": "error", "error": "branch_name must use an approved feature/agent prefix and cannot be main/master"}
            status = _git(["status", "--porcelain"])
            if status.returncode or status.stdout.strip():
                return {"status": "error", "error": "working tree is not clean; preserve and review existing changes before switching branches"}
            remotes = _git(["remote"])
            if remotes.returncode or "origin" not in remotes.stdout.splitlines():
                return {"status": "error", "error": "origin remote is not configured; do not guess or replace the repository URL"}
            fetch = _git(["fetch", "origin", "main"], timeout=60)
            if fetch.returncode:
                return {**_result(operation, fetch), "error": "fetch origin main failed; no branch was created"}
            for ref in (f"refs/heads/{branch_name}", f"refs/remotes/origin/{branch_name}"):
                exists = _git(["show-ref", "--verify", "--quiet", ref])
                if exists.returncode == 0:
                    return {"status": "error", "error": "branch name already exists locally or remotely; refusing to overwrite it"}
            created = _git(["switch", "--create", branch_name, "origin/main"])
            return _result(operation, created) | ({"branch": branch_name} if created.returncode == 0 else {})
        if operation == "stage":
            clean, path_error = _validate_paths(paths)
            if path_error:
                return {"status": "error", "error": path_error}
            result = _git(["add", "--", *clean])
            return _result(operation, result)
        if operation == "review":
            return _review()
        if operation == "commit":
            branch = _current_branch()
            if not branch or branch.lower() in {"main", "master"} or not _valid_branch_name(branch):
                return {"status": "error", "error": "commits to main/master are blocked; create a dedicated branch first"}
            if not confirm_reviewed_diff:
                return {"status": "error", "error": "confirm_reviewed_diff=true is required after reviewing the staged diff and test results"}
            clean, path_error = _validate_paths(paths)
            if path_error:
                return {"status": "error", "error": path_error}
            if not message or not message.strip() or len(message) > 160 or "\n" in message:
                return {"status": "error", "error": "message must be a single non-empty line of at most 160 characters"}
            staged = _staged_names()
            if staged is None or sorted(staged) != sorted(clean):
                return {"status": "error", "error": "commit paths must exactly match the staged paths; no unrelated staged files are included"}
            diff_check = _git(["diff", "--cached", "--check"])
            if diff_check.returncode:
                return {"status": "error", "error": "staged diff check failed", "details": _safe_text(diff_check.stdout + diff_check.stderr)}
            result = _git(["commit", "-m", message], timeout=60)
            return _result(operation, result)
        if operation == "push":
            branch = _current_branch()
            if not confirm_reviewed_diff:
                return {"status": "error", "error": "confirm_reviewed_diff=true is required after reviewing the commit and tests"}
            if not branch or branch.lower() in {"main", "master"} or branch_name != branch or not _valid_branch_name(branch):
                return {"status": "error", "error": "push is limited to the current dedicated branch; main/master and branch overrides are blocked"}
            status = _git(["status", "--porcelain"])
            if status.returncode or status.stdout.strip():
                return {"status": "error", "error": "working tree must be clean before push"}
            remotes = _git(["remote"])
            if remotes.returncode or "origin" not in remotes.stdout.splitlines():
                return {"status": "error", "error": "origin remote is not configured"}
            published = _git(["ls-remote", "--heads", "origin", branch], timeout=30)
            if published.returncode:
                return {"status": "error", "error": "could not inspect remote branch; push stopped"}
            upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"])
            if published.stdout.strip():
                if upstream.returncode or upstream.stdout.strip() != f"origin/{branch}":
                    return {"status": "error", "error": "remote branch exists but is not this branch's configured upstream; refusing to update a potentially shared branch"}
                result = _git(["push", "origin", branch], timeout=60)
            else:
                if upstream.returncode == 0 and upstream.stdout.strip() == f"origin/{branch}":
                    return {"status": "error", "error": "configured upstream disappeared; refusing to recreate it automatically"}
                result = _git(["push", "--set-upstream", "origin", branch], timeout=60)
            return _result(operation, result)
        if operation == "create_pr":
            branch = _current_branch()
            if not confirm_reviewed_diff:
                return {"status": "error", "error": "confirm_reviewed_diff=true is required after reviewing the branch and tests"}
            if not branch or branch.lower() in {"main", "master"} or branch_name != branch or not _valid_branch_name(branch):
                return {"status": "error", "error": "PR must originate from the current dedicated branch"}
            status = _git(["status", "--porcelain"])
            if status.returncode or status.stdout.strip():
                return {"status": "error", "error": "working tree must be clean before creating a PR"}
            if not title or not title.strip() or len(title) > 120 or not body or len(body) > 10_000:
                return {"status": "error", "error": "a PR title (<=120 chars) and body (<=10000 chars) are required"}
            published = _git(["ls-remote", "--heads", "origin", branch], timeout=30)
            if published.returncode or not published.stdout.strip():
                return {"status": "error", "error": "push the reviewed feature branch successfully before creating a PR"}
            auth = _run("gh", ["auth", "status"], timeout=15)
            if auth.returncode:
                return {"status": "error", "error": "GitHub CLI authentication unavailable; reconnect the authorized connector"}
            result = _run("gh", ["pr", "create", "--base", "main", "--head", branch,
                                  "--title", title.strip(), "--body", body], timeout=60)
            return _result(operation, result)
    except FileNotFoundError as exc:
        return {"status": "error", "error": str(exc), "automatic_install": False}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "operation timed out; inspect repository and provider state before retrying"}
    except OSError as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: operation could not be started"}
    return {"status": "error", "error": "operation did not complete"}
