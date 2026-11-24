from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import karry_agent.git_workspace as git_workspace
from karry_agent.git_workspace import run_git_command
from karry_agent.tools import build_default_registry


def git(*args, cwd: Path):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, shell=False)


def make_repo(path: Path, *, remote: Path | None = None) -> Path:
    path.mkdir(parents=True)
    git("init", "--initial-branch=main", cwd=path)
    git("config", "user.name", "Test Owner", cwd=path)
    git("config", "user.email", "owner@example.invalid", cwd=path)
    (path / "README.md").write_text("safe baseline\n", encoding="utf-8")
    git("add", "README.md", cwd=path)
    git("commit", "-m", "initial", cwd=path)
    if remote is not None:
        git("remote", "add", "origin", str(remote), cwd=path)
        git("push", "--set-upstream", "origin", "main", cwd=path)
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)
    path = make_repo(tmp_path / "repo", remote=origin)
    monkeypatch.setenv("KARRY_WORKSPACE", str(path))
    monkeypatch.setattr(git_workspace, "_remote_git",
                        lambda args, timeout=30: git_workspace._git(args, timeout=timeout))
    return path, origin


def test_git_tool_uses_explicit_operations_not_arbitrary_commands(repo):
    result = run_git_command("git status; touch injected")
    assert result["status"] == "error"
    assert not (repo[0] / "injected").exists()
    assert run_git_command("version")["status"] == "success"
    status = run_git_command("status")
    assert "## main" in status["stdout"]
    assert status["workspace_path"] == str(repo[0])


def test_initialize_requires_explicit_confirmation_and_never_starts_on_main(tmp_path, monkeypatch):
    root = tmp_path / "fresh"
    root.mkdir()
    monkeypatch.setenv("KARRY_WORKSPACE", str(root))
    refused = run_git_command("initialize")
    assert refused["status"] == "error"
    assert not (root / ".git").exists()
    result = run_git_command("initialize", branch_name="agent/import", confirm_initialization=True)
    assert result["status"] == "success"
    assert run_git_command("branch_info")["current_branch"] == "agent/import"
    assert run_git_command("initialize", branch_name="main", confirm_initialization=True)["status"] == "error"


def test_existing_origin_cannot_be_overwritten_and_remote_url_needs_confirmation(repo):
    refused = run_git_command("add_remote", remote_url="https://github.com/other/repo.git", confirm_remote_change=True)
    assert refused["status"] == "error" and "refusing to replace" in refused["error"]
    assert run_git_command("remotes")["status"] == "success"


def test_new_origin_requires_explicit_clean_github_url_and_confirmation(tmp_path, monkeypatch):
    path = make_repo(tmp_path / "repo")
    monkeypatch.setenv("KARRY_WORKSPACE", str(path))
    no_confirmation = run_git_command("add_remote", remote_url="https://github.com/owner/repo.git")
    assert no_confirmation["status"] == "error"
    credential_url = run_git_command("add_remote", remote_url="https://ghp_secret@github.com/owner/repo.git",
                                     confirm_remote_change=True)
    assert credential_url["status"] == "error"
    result = run_git_command("add_remote", remote_url="https://github.com/owner/repo.git",
                             confirm_remote_change=True)
    assert result["status"] == "success"
    assert "github.com/owner/repo" in result["repository_url"]


def test_remote_listing_redacts_credentials_embedded_in_urls(tmp_path, monkeypatch):
    path = make_repo(tmp_path / "repo")
    git("remote", "add", "origin", "https://ghp_" + "S" * 32 + "@github.com/owner/repo.git", cwd=path)
    monkeypatch.setenv("KARRY_WORKSPACE", str(path))
    result = run_git_command("remotes")
    assert result["status"] == "success"
    assert "ghp_" not in result["stdout"]
    assert "[redacted]@github.com" in result["stdout"]


def test_https_github_operations_use_per_command_gh_helper_without_global_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(git_workspace, "_git", lambda args, timeout=30:
                        subprocess.CompletedProcess(["git", *args], 0,
                                                    "https://github.com/owner/repo.git\n", ""))
    def fake_run(program, args, timeout=30):
        calls.append((program, args, timeout))
        return subprocess.CompletedProcess([program, *args], 0, "ok", "")
    monkeypatch.setattr(git_workspace, "_run", fake_run)
    result = git_workspace._remote_git(["push", "--set-upstream", "origin", "feature/demo"])
    assert result.returncode == 0
    args = calls[0][1]
    assert "credential.helper=" in args
    assert "credential.helper=!gh auth git-credential" in args
    assert "--set-upstream" in args


def test_remote_git_rejects_credentialed_or_non_github_https_origins(monkeypatch):
    for url in ("https://user:password@github.com/owner/repo.git", "https://gitlab.com/owner/repo.git"):
        monkeypatch.setattr(git_workspace, "_git", lambda args, timeout=30, url=url:
                            subprocess.CompletedProcess(["git", *args], 0, url + "\n", ""))
        result = git_workspace._remote_git(["push", "origin", "feature/demo"])
        assert result.returncode != 0
        assert "password" not in result.stderr


def test_create_branch_from_latest_main_requires_clean_workspace_and_unique_name(repo):
    path, origin = repo
    created = run_git_command("create_branch", branch_name="feature/safe-demo")
    assert created["status"] == "success"
    assert created["branch"] == "feature/safe-demo"
    assert run_git_command("branch_info")["current_branch"] == "feature/safe-demo"
    duplicate = run_git_command("create_branch", branch_name="feature/safe-demo")
    assert duplicate["status"] == "error"
    assert "already exists" in duplicate["error"]
    assert run_git_command("create_branch", branch_name="feature/../main")["status"] == "error"
    (path / "dirty.txt").write_text("leave untouched", encoding="utf-8")
    assert run_git_command("create_branch", branch_name="feature/another")["status"] == "error"
    assert (path / "dirty.txt").exists()


def test_stage_is_explicit_and_rejects_secrets_globs_and_escape_paths(repo):
    path, _ = repo
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    assert run_git_command("stage", paths=["."])["status"] == "error"
    assert run_git_command("stage", paths=["*.py"])["status"] == "error"
    assert run_git_command("stage", paths=[".env"])["status"] == "error"
    assert run_git_command("stage", paths=["../outside.txt"])["status"] == "error"
    staged = run_git_command("stage", paths=["src/app.py"])
    assert staged["status"] == "success"
    assert "src/app.py" in run_git_command("review")["staged_paths"]


def test_commit_requires_review_exact_staged_paths_and_feature_branch(repo):
    path, _ = repo
    assert run_git_command("create_branch", branch_name="feature/commit-test")["status"] == "success"
    (path / "src.py").write_text("value = 42\n", encoding="utf-8")
    assert run_git_command("stage", paths=["src.py"])["status"] == "success"
    assert run_git_command("commit", paths=["src.py"], message="add source")["status"] == "error"
    mismatch = run_git_command("commit", paths=["README.md"], message="add source", confirm_reviewed_diff=True)
    assert mismatch["status"] == "error"
    committed = run_git_command("commit", paths=["src.py"], message="add source",
                                confirm_reviewed_diff=True)
    assert committed["status"] == "success"
    assert "add source" in git("log", "-1", "--format=%s", cwd=path).stdout


def test_push_never_targets_main_and_allows_only_non_force_feature_pushes(repo):
    path, origin = repo
    blocked = run_git_command("push", branch_name="main", confirm_reviewed_diff=True)
    assert blocked["status"] == "error"
    assert run_git_command("create_branch", branch_name="feat/publish-demo")["status"] == "success"
    (path / "demo.txt").write_text("demo\n", encoding="utf-8")
    run_git_command("stage", paths=["demo.txt"])
    run_git_command("commit", paths=["demo.txt"], message="add demo", confirm_reviewed_diff=True)
    unconfirmed = run_git_command("push", branch_name="feat/publish-demo")
    assert unconfirmed["status"] == "error"
    pushed = run_git_command("push", branch_name="feat/publish-demo", confirm_reviewed_diff=True)
    assert pushed["status"] == "success"
    refs = git("show-ref", cwd=origin).stdout
    assert "refs/heads/feat/publish-demo" in refs
    (path / "demo.txt").write_text("demo updated\n", encoding="utf-8")
    run_git_command("stage", paths=["demo.txt"])
    run_git_command("commit", paths=["demo.txt"], message="update demo", confirm_reviewed_diff=True)
    assert run_git_command("push", branch_name="feat/publish-demo", confirm_reviewed_diff=True)["status"] == "success"


def test_diff_review_redacts_provider_tokens_and_private_keys(repo):
    path, _ = repo
    run_git_command("create_branch", branch_name="feature/redact-diff")
    token = "ghp_" + "A" * 32
    header = "-" * 5 + "BEGIN TEST " + "PRIVATE KEY" + "-" * 5
    footer = "-" * 5 + "END TEST " + "PRIVATE KEY" + "-" * 5
    secret = header + "\n" + "B" * 9000 + "\n" + footer
    (path / "notes.txt").write_text(f"token={token}\n{secret}\n", encoding="utf-8")
    run_git_command("stage", paths=["notes.txt"])
    review = run_git_command("review")
    assert review["status"] == "success"
    assert token not in review["review_diff"]
    assert "BEGIN TEST PRIVATE KEY" not in review["review_diff"]
    assert "[redacted]" in review["review_diff"]
    assert "[redacted private key]" in review["review_diff"]


def test_registry_exposes_safe_git_tool_without_shell_command_parameter():
    registry = build_default_registry()
    declaration = registry.tools["run_git_command"].declaration
    assert declaration["parameters"]["required"] == ["operation"]
    assert "command" not in declaration["parameters"]["properties"]
    result = registry.execute("run_git_command", {"operation": "git push origin main"})
    assert result["status"] == "error"


def test_pr_creation_requires_review_confirmation_before_gh_invocation(repo):
    run_git_command("create_branch", branch_name="feature/no-unconfirmed-pr")
    result = run_git_command("create_pr", branch_name="feature/no-unconfirmed-pr", title="Test",
                             body="Test PR", confirm_reviewed_diff=False)
    assert result["status"] == "error"
    assert "confirm_reviewed_diff" in result["error"]


def _fake_gh(monkeypatch, *, auth_ok=True):
    calls = []
    real_run = git_workspace._run

    def run(program, args, *, timeout=30):
        if program != "gh":
            return real_run(program, args, timeout=timeout)
        calls.append(args)
        code = 0 if auth_ok or args[:2] != ["auth", "status"] else 1
        return subprocess.CompletedProcess(["gh", *args], code, "https://github.com/o/r/pull/1\n" if code == 0 else "", "")

    monkeypatch.setattr(git_workspace, "_run", run)
    return calls


def _pushed_feature(path, branch="feature/pr-ready"):
    assert run_git_command("create_branch", branch_name=branch)["status"] == "success"
    (path / "feature.txt").write_text("feature\n", encoding="utf-8")
    run_git_command("stage", paths=["feature.txt"])
    run_git_command("commit", paths=["feature.txt"], message="add feature", confirm_reviewed_diff=True)
    assert run_git_command("push", branch_name=branch, confirm_reviewed_diff=True)["status"] == "success"
    return branch


def create_pr(branch, **overrides):
    return run_git_command("create_pr", **{"branch_name": branch, "title": "Add feature", "body": "Tested.",
                                           "confirm_reviewed_diff": True, **overrides})


def test_pr_is_created_from_the_pushed_feature_branch_against_main(repo, monkeypatch):
    path, _ = repo
    branch = _pushed_feature(path)
    calls = _fake_gh(monkeypatch)
    result = create_pr(branch, title="  Add feature  ")
    assert result["status"] == "success" and "pull/1" in result["stdout"]
    assert calls[-1] == ["pr", "create", "--base", "main", "--head", branch, "--title", "Add feature", "--body", "Tested."]


@pytest.mark.parametrize("overrides,error", [
    ({"branch_name": "feature/other"}, "current dedicated branch"),
    ({"title": "   "}, "title"),
    ({"title": "x" * 121}, "title"),
    ({"body": ""}, "body"),
    ({"body": "x" * 10_001}, "body"),
])
def test_pr_creation_rejects_wrong_branch_or_missing_metadata_without_gh(repo, monkeypatch, overrides, error):
    path, _ = repo
    branch = _pushed_feature(path)
    calls = _fake_gh(monkeypatch)
    result = create_pr(branch, **overrides)
    assert result["status"] == "error" and error in result["error"]
    assert calls == []


def test_pr_creation_requires_clean_tree_pushed_branch_and_gh_auth(repo, monkeypatch):
    path, _ = repo
    calls = _fake_gh(monkeypatch)
    run_git_command("create_branch", branch_name="feature/not-pushed")
    assert "push the reviewed feature branch" in create_pr("feature/not-pushed")["error"]
    (path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    assert "working tree must be clean" in create_pr("feature/not-pushed")["error"]
    assert calls == []
    (path / "dirty.txt").unlink()
    git("checkout", "-q", "main", cwd=path)
    branch = _pushed_feature(path, "feature/auth-check")
    calls = _fake_gh(monkeypatch, auth_ok=False)
    result = create_pr(branch)
    assert result["status"] == "error" and "authentication unavailable" in result["error"]
    assert calls == [["auth", "status"]]


def test_pr_creation_is_blocked_on_main(repo, monkeypatch):
    calls = _fake_gh(monkeypatch)
    assert create_pr("main")["status"] == "error"
    assert calls == []


def test_push_refuses_a_remote_branch_it_does_not_track(repo):
    path, _ = repo
    run_git_command("create_branch", branch_name="feature/shared")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    run_git_command("stage", paths=["a.txt"])
    run_git_command("commit", paths=["a.txt"], message="a", confirm_reviewed_diff=True)
    git("push", "-q", "origin", "feature/shared", cwd=path)  # published by someone else, no upstream here
    result = run_git_command("push", branch_name="feature/shared", confirm_reviewed_diff=True)
    assert result["status"] == "error" and "not this branch's configured upstream" in result["error"]


def test_push_does_not_recreate_a_deleted_upstream(repo):
    path, origin = repo
    branch = _pushed_feature(path, "feature/deleted-upstream")
    git("branch", "-D", branch, cwd=origin)
    result = run_git_command("push", branch_name=branch, confirm_reviewed_diff=True)
    assert result["status"] == "error" and "upstream disappeared" in result["error"]
    assert branch not in git("branch", "--list", cwd=origin).stdout


def test_push_requires_a_clean_tree(repo):
    path, _ = repo
    branch = _pushed_feature(path, "feature/dirty-push")
    (path / "feature.txt").write_text("changed\n", encoding="utf-8")
    result = run_git_command("push", branch_name=branch, confirm_reviewed_diff=True)
    assert result["status"] == "error" and "clean" in result["error"]


@pytest.mark.parametrize("failure,error", [
    (subprocess.TimeoutExpired(["git"], 30), "timed out"),
    (FileNotFoundError("git is not installed"), "git is not installed"),
    (PermissionError("denied"), "PermissionError"),
])
def test_process_failures_are_reported_not_raised(repo, monkeypatch, failure, error):
    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(git_workspace, "_git", broken)
    result = run_git_command("status")
    assert result["status"] == "error" and error in result["error"]
