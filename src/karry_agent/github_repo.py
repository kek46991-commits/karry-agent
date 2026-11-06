"""Allowlisted GitHub repository automation over the REST API.

Works without a local checkout, `gh` or stored Git credentials, so it also runs on Render.
Agent changes land as one commit on a fresh `agent/*` branch, open a PR against the base
branch, and are merged only when `KARRY_GIT_AUTO_MERGE=1`, every CI check passed and the
PR head still equals the commit this module created (SHA-pinned merge).
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import time
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import quote

import requests

from .git_workspace import (
    _is_protected,
    _root,
    _safe_text,
    _valid_branch_name,
    _validate_paths,
)
from .optimus_core import PROTECTED_PATHS as _PROTECTED_SOURCES

API = "https://api.github.com"
BRANCH_PREFIX = "agent/"
MAX_FILES = 20
MAX_FILE_BYTES = 200_000
_PASSING = {"success", "neutral", "skipped"}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PROTECTED_PREFIXES = (".github/", ".devin/", ".agents/")
_PROTECTED_FILES = frozenset({"render.yaml", "Dockerfile", "requirements.txt", "pyproject.toml",
                              "package.json", "package-lock.json", ".env.example",
                              "src/karry_agent/github_repo.py", "src/karry_agent/git_workspace.py",
                              "src/karry_agent/auth_token.py", "src/karry_agent/feature_flags.py",
                              "src/karry_agent/github_workflow.py"}) | _PROTECTED_SOURCES
_CREDENTIAL_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9_]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\bAIza[A-Za-z0-9_-]{30,}|\brnd_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
)
_SECRET_ENV_RE = re.compile(r"token|secret|password|api.?key|passphrase|credential|dsn|database_url", re.IGNORECASE)


class GitHubRepoError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def auto_merge_enabled() -> bool:
    return _flag("KARRY_GIT_AUTO_MERGE")


def allowed_repos() -> tuple[str, ...]:
    repos = (r.strip() for r in os.getenv("KARRY_GIT_ALLOWED_REPOS", "").split(","))
    return tuple(dict.fromkeys(r for r in repos if _REPO_RE.fullmatch(r)))


def base_branch() -> str:
    return os.getenv("KARRY_GIT_BASE_BRANCH", "main").strip() or "main"


def check_timeout() -> int:
    try:
        return max(0, min(int(os.getenv("KARRY_GIT_CHECK_TIMEOUT", "600")), 1800))
    except ValueError:
        return 600


def protected_path(path: str) -> bool:
    return (_is_protected(path) or path in _PROTECTED_FILES or path.startswith(_PROTECTED_PREFIXES)
            or path.startswith(("/", "-")) or ".." in path.split("/") or "\\" in path)


def contains_secret(text: str) -> bool:
    if _CREDENTIAL_RE.search(text):
        return True
    values = (v.strip() for k, v in os.environ.items() if _SECRET_ENV_RE.search(k))
    return any(len(v) >= 8 and v in text for v in values)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return slug or "change"


def new_branch_name(title: str) -> str:
    return f"{BRANCH_PREFIX}{_slug(title)}-{date.today():%Y%m%d}-{secrets.token_hex(3)}"


def validated_files(files: list[dict[str, Any]] | None) -> dict[str, str]:
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise GitHubRepoError(f"files must list 1–{MAX_FILES} {{path, content}} entries", "invalid_files")
    clean: dict[str, str] = {}
    for item in files:
        path = item.get("path") if isinstance(item, dict) else None
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(path, str) or not isinstance(content, str) or not path.strip():
            raise GitHubRepoError("each file needs a string path and content", "invalid_files")
        path = path.strip()
        path = path.removeprefix("./")
        if protected_path(path):
            raise GitHubRepoError(f"protected path cannot be changed automatically: {path}", "protected_path")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise GitHubRepoError(f"file is too large: {path}", "file_too_large")
        if contains_secret(content):
            raise GitHubRepoError(f"content looks like it contains a credential: {path}", "secret_detected")
        clean[path] = content
    return clean


def workspace_files(paths: list[str] | None) -> list[dict[str, str]]:
    clean, error = _validate_paths(paths)
    if error:
        raise GitHubRepoError(error, "invalid_paths")
    root = _root()
    out = []
    for path in clean:
        target = root / path
        if not target.is_file():
            raise GitHubRepoError(f"workspace file not found: {path}", "invalid_paths")
        out.append({"path": path, "content": target.read_text(encoding="utf-8")})
    return out


class GitHubRepo:
    """REST client limited to branch/commit/PR/check/merge calls for one allowlisted repo."""

    def __init__(self, repo: str, token: str, *, base: str = "main", session: Any = None) -> None:
        if repo not in allowed_repos():
            raise GitHubRepoError("repository is not in KARRY_GIT_ALLOWED_REPOS", "repo_not_allowed")
        if not token:
            raise GitHubRepoError("KARRY_GIT_GITHUB_TOKEN is not configured", "github_not_configured")
        if not re.fullmatch(r"[\w./-]+", base) or ".." in base:
            raise GitHubRepoError("invalid base branch", "invalid_base")
        self.repo, self.base = repo, base
        self.root = f"{API}/repos/{repo}"
        self.session = session or requests.Session()
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.session.request(method, self.root + path, headers=self.headers,
                                            timeout=(5, 30), allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise GitHubRepoError(f"GitHub request failed: {type(exc).__name__}", "github_unreachable") from None
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("message", ""))
            except ValueError:
                detail = ""
            raise GitHubRepoError(_safe_text(f"GitHub {method} {path.split('?')[0]} -> "
                                             f"{response.status_code} {detail}")[:300],
                                  f"github_{response.status_code}")
        return response.json() if response.content else {}

    def commit_files(self, branch: str, files: dict[str, str], message: str) -> dict[str, str]:
        """Create `branch` from the base head with all files in a single commit."""
        if not _valid_branch_name(branch) or not branch.startswith(BRANCH_PREFIX):
            raise GitHubRepoError("branch must be a valid agent/* name", "invalid_branch")
        head = self._call("GET", "/git/ref/heads/" + quote(self.base, safe=""))["object"]["sha"]
        base_tree = self._call("GET", f"/git/commits/{head}")["tree"]["sha"]
        tree = [{"path": path, "mode": "100644", "type": "blob",
                 "sha": self._call("POST", "/git/blobs", json={
                     "content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})["sha"]}
                for path, content in files.items()]
        tree_sha = self._call("POST", "/git/trees", json={"base_tree": base_tree, "tree": tree})["sha"]
        commit = self._call("POST", "/git/commits", json={"message": message, "tree": tree_sha,
                                                          "parents": [head]})["sha"]
        self._call("POST", "/git/refs", json={"ref": "refs/heads/" + branch, "sha": commit})
        return {"base_sha": head, "commit_sha": commit}

    def open_pr(self, branch: str, title: str, body: str) -> dict[str, Any]:
        pr = self._call("POST", "/pulls", json={"title": title, "head": branch, "base": self.base,
                                                "body": body, "draft": False})
        return {"pr_number": pr["number"], "pr_url": pr["html_url"]}

    def pull_request(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/pulls/{int(number)}")

    def pr_files(self, number: int) -> list[str]:
        return [f["filename"] for f in self._call("GET", f"/pulls/{int(number)}/files", params={"per_page": 100})]

    def checks(self, sha: str) -> dict[str, Any]:
        runs = self._call("GET", f"/commits/{sha}/check-runs", params={"per_page": 100}).get("check_runs", [])
        pending = [r["name"] for r in runs if r.get("status") != "completed"]
        failed = [r["name"] for r in runs if r.get("status") == "completed" and r.get("conclusion") not in _PASSING]
        # No reported checks is not a pass: fail closed until CI reports.
        state = "pending" if pending or not runs else "failed" if failed else "passed"
        return {"state": state, "total": len(runs), "pending": pending, "failed": failed}

    def wait_for_checks(self, sha: str, timeout: int, sleep: Callable[[float], None] = time.sleep,
                        interval: float = 15) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            summary = self.checks(sha)
            if summary["state"] != "pending" or time.monotonic() >= deadline:
                return summary
            sleep(interval)

    def merge(self, number: int, sha: str, title: str) -> dict[str, Any]:
        result = self._call("PUT", f"/pulls/{int(number)}/merge",
                            json={"sha": sha, "merge_method": "squash", "commit_title": title[:200]})
        return {"merged": bool(result.get("merged")), "merge_sha": result.get("sha")}

    def delete_branch(self, branch: str) -> bool:
        try:
            self._call("DELETE", "/git/refs/heads/" + quote(branch, safe="/"))
            return True
        except GitHubRepoError:
            return False


def client_from_env(repo: str | None, session: Any = None) -> GitHubRepo:
    repos = allowed_repos()
    target = (repo or (repos[0] if len(repos) == 1 else "")).strip()
    if not target:
        raise GitHubRepoError("repo is required (owner/name from KARRY_GIT_ALLOWED_REPOS)", "repo_required")
    return GitHubRepo(target, os.getenv("KARRY_GIT_GITHUB_TOKEN", "").strip(), base=base_branch(), session=session)


def _merge_if_safe(gh: GitHubRepo, number: int, expected_sha: str, title: str) -> dict[str, Any]:
    """Merge only an open agent/* PR whose head is still the expected, CI-green commit."""
    if not auto_merge_enabled():
        return {"merged": False, "reason": "auto_merge_disabled"}
    pr = gh.pull_request(number)
    head = pr.get("head") or {}
    if pr.get("state") != "open" or pr.get("draft"):
        return {"merged": False, "reason": "pr_not_open"}
    if (pr.get("base") or {}).get("ref") != gh.base or not str(head.get("ref", "")).startswith(BRANCH_PREFIX):
        return {"merged": False, "reason": "not_an_agent_pr"}
    if (head.get("repo") or {}).get("full_name") != gh.repo:
        return {"merged": False, "reason": "fork_pr_refused"}
    if head.get("sha") != expected_sha:
        return {"merged": False, "reason": "head_changed"}
    blocked = [p for p in gh.pr_files(number) if protected_path(p)]
    if blocked:
        return {"merged": False, "reason": "protected_path", "paths": blocked}
    checks = gh.checks(expected_sha)
    if checks["state"] != "passed":
        return {"merged": False, "reason": "checks_" + checks["state"], "checks": checks}
    result = gh.merge(number, expected_sha, title)
    if result["merged"]:
        result["branch_deleted"] = gh.delete_branch(head["ref"])
    return result


def ship(repo: str | None, title: str, files: list[dict[str, Any]], *, body: str = "", message: str | None = None,
         wait_for_checks: bool = True, merge: bool = True, session: Any = None,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Branch → one commit → PR → wait for CI → SHA-pinned merge (when enabled)."""
    title = (title or "").strip()
    if not title or len(title) > 120 or "\n" in title:
        raise GitHubRepoError("title must be one line of 1–120 characters", "invalid_title")
    if len(body) > 10_000:
        raise GitHubRepoError("body is too long", "invalid_body")
    clean = validated_files(files)
    gh = client_from_env(repo, session)
    branch = new_branch_name(title)
    commit = gh.commit_files(branch, clean, (message or title).strip()[:200])
    pr = gh.open_pr(branch, title, (body.strip() + "\n\n" if body.strip() else "")
                    + "Opened automatically by Karry-Agent (`github_repository` ship).")
    result: dict[str, Any] = {"status": "pr_created", "repo": gh.repo, "base": gh.base, "branch": branch,
                              "paths": sorted(clean), **commit, **pr, "merged": False}
    if not wait_for_checks:
        return result
    try:
        result["checks"] = gh.wait_for_checks(commit["commit_sha"], check_timeout(), sleep)
        if not merge:
            return result
        outcome = _merge_if_safe(gh, pr["pr_number"], commit["commit_sha"], title)
    except GitHubRepoError as exc:
        # The PR already exists; report it instead of losing it behind an exception.
        outcome = {"merged": False, "reason": "github_error", "error": str(exc), "code": exc.code}
    result.update(outcome)
    result["status"] = "merged" if outcome.get("merged") else "pr_open"
    return result


def run_github_repository(operation: str, repo: str | None = None, title: str | None = None,
                          body: str | None = None, message: str | None = None,
                          files: list[dict[str, Any]] | None = None, paths: list[str] | None = None,
                          pr_number: int | None = None, expected_sha: str | None = None,
                          wait_for_checks: bool = True, merge: bool = True,
                          confirm_merge: bool = False) -> dict[str, Any]:
    """Agent tool entry point: one enumerated operation, never a shell string."""
    try:
        if operation == "status":
            return {"status": "success", "configured": bool(os.getenv("KARRY_GIT_GITHUB_TOKEN", "").strip()),
                    "allowed_repos": list(allowed_repos()), "base_branch": base_branch(),
                    "auto_merge_enabled": auto_merge_enabled(), "check_timeout_seconds": check_timeout()}
        if operation == "ship":
            if files is None and paths is not None:
                files = workspace_files(paths)
            return ship(repo, title or "", files or [], body=body or "", message=message,
                        wait_for_checks=wait_for_checks, merge=merge)
        if operation in {"pr_status", "merge_pr"}:
            if not isinstance(pr_number, int) or pr_number < 1:
                return {"status": "error", "error": "pr_number is required"}
            gh = client_from_env(repo)
            pr = gh.pull_request(pr_number)
            sha = (pr.get("head") or {}).get("sha", "")
            if operation == "pr_status":
                return {"status": "success", "pr_number": pr_number, "state": pr.get("state"),
                        "merged": bool(pr.get("merged")), "head_sha": sha,
                        "head_branch": (pr.get("head") or {}).get("ref"), "checks": gh.checks(sha)}
            if not confirm_merge or not expected_sha:
                return {"status": "error", "error": "confirm_merge=true and expected_sha (reviewed head SHA) are required"}
            outcome = _merge_if_safe(gh, pr_number, expected_sha, pr.get("title") or f"PR #{pr_number}")
            return {"status": "merged" if outcome.get("merged") else "not_merged", "pr_number": pr_number, **outcome}
        return {"status": "error", "error": "operation is not allowed",
                "allowed_operations": ["status", "ship", "pr_status", "merge_pr"]}
    except GitHubRepoError as exc:
        return {"status": "error", "error": str(exc), "code": exc.code}
