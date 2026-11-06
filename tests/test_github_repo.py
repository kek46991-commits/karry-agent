from __future__ import annotations

import base64

import pytest

import karry_agent.github_repo as gr
from karry_agent.github_repo import GitHubRepoError, run_github_repository, ship
from karry_agent.tools import build_default_registry

REPO = "owner/demo"


class Resp:
    def __init__(self, status: int, data=None):
        self.status_code, self._data = status, data
        self.content = b"x" if data is not None else b""

    def json(self):
        return self._data


class FakeGitHub:
    """Minimal in-memory GitHub REST API for branches, commits, PRs, checks and merges."""

    def __init__(self, checks=None):
        self.refs = {"main": "base0"}
        self.commits = {"base0": {"tree": "tree0", "files": {"README.md": "hi\n"}}}
        self.blobs, self.trees, self.prs, self.calls = {}, {}, {}, []
        self.check_runs = checks if checks is not None else [{"name": "test", "status": "completed", "conclusion": "success"}]
        self.head_override = None
        self.merge_status = 200

    def request(self, method, url, headers=None, timeout=None, allow_redirects=None, json=None, params=None):
        assert headers["Authorization"] == "Bearer test-token-value"
        assert allow_redirects is False
        path = url.removeprefix(f"https://api.github.com/repos/{REPO}")
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/git/ref/heads/"):
            return Resp(200, {"object": {"sha": self.refs[path.rsplit("/", 1)[1]]}})
        if method == "GET" and path.startswith("/git/commits/"):
            return Resp(200, {"tree": {"sha": self.commits[path.rsplit("/", 1)[1]]["tree"]}})
        if method == "POST" and path == "/git/blobs":
            sha = f"blob{len(self.blobs)}"
            self.blobs[sha] = base64.b64decode(json["content"]).decode()
            return Resp(201, {"sha": sha})
        if method == "POST" and path == "/git/trees":
            sha = f"tree{len(self.trees) + 1}"
            self.trees[sha] = json
            return Resp(201, {"sha": sha})
        if method == "POST" and path == "/git/commits":
            sha = f"commit{len(self.commits)}"
            base_files = next(c["files"] for c in self.commits.values() if c["tree"] == self.trees[json["tree"]]["base_tree"])
            files = {**base_files, **{t["path"]: self.blobs[t["sha"]] for t in self.trees[json["tree"]]["tree"]}}
            self.commits[sha] = {"tree": json["tree"], "files": files, "parents": json["parents"], "message": json["message"]}
            return Resp(201, {"sha": sha})
        if method == "POST" and path == "/git/refs":
            self.refs[json["ref"].removeprefix("refs/heads/")] = json["sha"]
            return Resp(201, {})
        if method == "DELETE" and path.startswith("/git/refs/heads/"):
            self.refs.pop(path.removeprefix("/git/refs/heads/"))
            return Resp(204)
        if method == "POST" and path == "/pulls":
            number = len(self.prs) + 1
            self.prs[number] = {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
                                "title": json["title"], "state": "open", "draft": False, "merged": False,
                                "base": {"ref": json["base"]}, "head": {"ref": json["head"], "repo": {"full_name": REPO}}}
            return Resp(201, self.prs[number])
        if method == "GET" and path.startswith("/pulls/") and path.endswith("/files"):
            pr = self.prs[int(path.split("/")[2])]
            base = self.commits[self.refs[pr["base"]["ref"]]]["files"]
            head = self.commits[self.refs[pr["head"]["ref"]]]["files"]
            return Resp(200, [{"filename": p} for p in head if base.get(p) != head[p]])
        if method == "GET" and path.startswith("/pulls/"):
            pr = dict(self.prs[int(path.split("/")[2])])
            pr["head"] = {**pr["head"], "sha": self.head_override or self.refs.get(pr["head"]["ref"])}
            return Resp(200, pr)
        if method == "GET" and path.endswith("/check-runs"):
            return Resp(200, {"check_runs": self.check_runs})
        if method == "PUT" and path.endswith("/merge"):
            if self.merge_status != 200:
                return Resp(self.merge_status, {"message": "Head branch was modified"})
            pr = self.prs[int(path.split("/")[2])]
            assert json["sha"] == self.refs[pr["head"]["ref"]]
            self.refs["main"] = json["sha"]
            pr["state"], pr["merged"] = "closed", True
            return Resp(200, {"merged": True, "sha": json["sha"]})
        return Resp(404, {"message": "Not Found"})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("KARRY_GIT_GITHUB_TOKEN", "test-token-value")
    monkeypatch.setenv("KARRY_GIT_ALLOWED_REPOS", REPO)
    monkeypatch.setenv("KARRY_GIT_AUTO_MERGE", "1")
    monkeypatch.setenv("KARRY_GIT_CHECK_TIMEOUT", "0")
    monkeypatch.delenv("KARRY_GIT_BASE_BRANCH", raising=False)


def run_ship(fake, **kw):
    return ship(REPO, "Add greeting doc", [{"path": "docs/hello.md", "content": "# Hello\n"}],
                session=fake, sleep=lambda _: None, **kw)


def test_ship_branches_commits_opens_pr_and_merges_after_green_ci(env):
    fake = FakeGitHub()
    result = run_ship(fake)
    assert result["status"] == "merged" and result["merged"] is True
    assert result["branch"].startswith("agent/add-greeting-doc-")
    assert fake.refs["main"] == result["commit_sha"] == result["merge_sha"]
    assert fake.commits[result["commit_sha"]]["files"] == {"README.md": "hi\n", "docs/hello.md": "# Hello\n"}
    assert fake.commits[result["commit_sha"]]["parents"] == ["base0"]
    assert result["branch"] not in fake.refs and result["branch_deleted"] is True
    assert not any(m == "PATCH" or (m == "POST" and "/git/refs" in p and "main" in p) for m, p in fake.calls)


@pytest.mark.parametrize("runs,reason", [
    ([], "checks_pending"),
    ([{"name": "test", "status": "in_progress"}], "checks_pending"),
    ([{"name": "test", "status": "completed", "conclusion": "failure"}], "checks_failed"),
])
def test_ship_never_merges_without_passing_ci(env, runs, reason):
    fake = FakeGitHub(checks=runs)
    result = run_ship(fake)
    assert result["status"] == "pr_open" and result["merged"] is False and result["reason"] == reason
    assert fake.refs["main"] == "base0"


def test_ship_respects_auto_merge_flag_and_merge_false(env, monkeypatch):
    fake = FakeGitHub()
    assert run_ship(fake, merge=False)["status"] == "pr_created"
    assert fake.refs["main"] == "base0"
    monkeypatch.delenv("KARRY_GIT_AUTO_MERGE")
    result = run_ship(FakeGitHub())
    assert result["reason"] == "auto_merge_disabled" and result["merged"] is False


@pytest.fixture
def use_fake(monkeypatch):
    def install(fake):
        original = gr.client_from_env
        monkeypatch.setattr(gr, "client_from_env", lambda repo, session=None: original(repo, fake))
        return fake
    return install


def test_merge_refuses_when_head_moved_after_review(env, use_fake):
    fake = use_fake(FakeGitHub())
    first = run_ship(fake, merge=False)
    fake.head_override = "someone-else"
    result = run_github_repository("merge_pr", repo=REPO, pr_number=first["pr_number"],
                                   expected_sha=first["commit_sha"], confirm_merge=True)
    assert result["status"] == "not_merged" and result["reason"] == "head_changed"
    assert fake.refs["main"] == "base0"


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "render.yaml", ".env", "app.py",
                                  "src/karry_agent/auth_token.py", "../escape.py", "/etc/passwd",
                                  "keys/id_rsa", "src/karry_agent/github_repo.py", "requirements.txt"])
def test_protected_paths_are_rejected_before_any_github_call(env, path):
    fake = FakeGitHub()
    with pytest.raises(GitHubRepoError) as err:
        ship(REPO, "x", [{"path": path, "content": "x"}], session=fake)
    assert err.value.code in {"protected_path", "invalid_files"}
    assert fake.calls == []


def test_credentials_in_content_are_rejected(env, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "super-secret-value-123")
    for content in ("token = 'ghp_" + "a" * 36 + "'", "key=super-secret-value-123", "test-token-value"):
        with pytest.raises(GitHubRepoError) as err:
            ship(REPO, "x", [{"path": "docs/a.md", "content": content}], session=FakeGitHub())
        assert err.value.code == "secret_detected"


def test_repository_allowlist_and_missing_token(env, monkeypatch):
    with pytest.raises(GitHubRepoError) as err:
        ship("someone/else", "x", [{"path": "docs/a.md", "content": "a"}], session=FakeGitHub())
    assert err.value.code == "repo_not_allowed"
    monkeypatch.setenv("KARRY_GIT_GITHUB_TOKEN", "  ")
    assert run_github_repository("ship", repo=REPO, title="x", files=[{"path": "docs/a.md", "content": "a"}])["code"] == "github_not_configured"


def test_merge_pr_requires_explicit_confirmation_and_sha(env, use_fake):
    fake = use_fake(FakeGitHub())
    first = run_ship(fake, merge=False)
    assert run_github_repository("merge_pr", repo=REPO, pr_number=first["pr_number"])["status"] == "error"
    status = run_github_repository("pr_status", repo=REPO, pr_number=first["pr_number"])
    assert status["checks"]["state"] == "passed" and status["head_sha"] == first["commit_sha"]
    merged = run_github_repository("merge_pr", repo=REPO, pr_number=first["pr_number"],
                                   expected_sha=status["head_sha"], confirm_merge=True)
    assert merged["status"] == "merged" and fake.refs["main"] == first["commit_sha"]


def test_rejected_merge_keeps_pr_info_and_hides_token(env):
    fake = FakeGitHub()
    fake.merge_status = 409
    result = run_ship(fake)
    assert result["status"] == "pr_open" and result["merged"] is False and result["code"] == "github_409"
    assert result["pr_url"].endswith("/pull/1") and "test-token-value" not in str(result)
    assert fake.refs["main"] == "base0"


def test_unknown_operation_and_status(env):
    assert run_github_repository("rm -rf /")["status"] == "error"
    status = run_github_repository("status")
    assert status["configured"] is True and status["allowed_repos"] == [REPO] and status["auto_merge_enabled"] is True
    assert "test-token-value" not in str(status)


def test_tool_is_registered_with_operation_enum():
    tool = build_default_registry().tools["github_repository"]
    assert tool.declaration["parameters"]["properties"]["operation"]["enum"] == ["status", "ship", "pr_status", "merge_pr"]
