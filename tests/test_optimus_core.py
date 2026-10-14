import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient

import app as web
from karry_agent import optimus_core as optimus
from karry_agent.optimus_core import CloneRegistry, OptimusCore, OptimusError, OptimusGitHub

TOKEN = "test-owner-optimus"
CORE = "src/karry_agent/core.py"
ORIGINAL = "VALUE = 1\n"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ("KARRY_OPTIMUS_GITHUB_TOKEN", "KARRY_OPTIMUS_AUTO_MERGE", "KARRY_OPTIMUS_PATHS",
                "KARRY_OPTIMUS_MAX_CLONES", "KARRY_OPTIMUS_MAX_GENERATIONS", "KARRY_ALLOW_SHELL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "src/karry_agent").mkdir(parents=True)
    (tmp_path / CORE).write_text(ORIGINAL, encoding="utf-8")
    (tmp_path / "app.py").write_text("SECURE = True\n", encoding="utf-8")
    return tmp_path


class FakeGitHub:
    base, repo = "main", optimus.DEFAULT_REPO

    def __init__(self):
        self.opened, self.merged = [], []
        self.pr = {"number": 7, "state": "open", "merged": False, "draft": False, "mergeable": True,
                   "title": "Optimus Core: test", "html_url": "https://github.com/x/y/pull/7",
                   "head": {"ref": "optimus/abc", "sha": "h" * 40, "repo": {"full_name": self.repo}},
                   "base": {"ref": "main"}}
        self.files = [CORE]
        self.checks = {"state": "passed", "total": 2, "pending": [], "failed": []}

    def open_pull_request(self, branch, title, body, changes, originals):
        self.opened.append((branch, title, body, changes, originals))
        return {"pr_number": 7, "pr_url": self.pr["html_url"], "branch": branch, "head_sha": "h" * 40}

    def pull_request(self, number):
        return self.pr

    def pull_request_files(self, number):
        return self.files

    def check_summary(self, sha):
        return self.checks

    def merge(self, number, sha, title):
        self.merged.append((number, sha))
        return {"sha": "m" * 40, "merged": True}


def edit(new="VALUE = 2\n", path=CORE, old=ORIGINAL):
    return [{"path": path, "old": old, "new": new}]


def auth():
    return {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def client(root, monkeypatch):
    github = FakeGitHub()
    core = OptimusCore(root, github_factory=lambda: github, generator=lambda goal, sources: {
        "summary": "生成された改善", "edits": edit("VALUE = 3\n")})
    monkeypatch.setattr(web, "_OPTIMUS", core)
    web.app.dependency_overrides.clear()
    yield TestClient(web.app), github, core
    web.app.dependency_overrides.clear()


# ----------------------------------------------------------------- clones

def test_clones_are_named_kerry_from_generation_two():
    registry = CloneRegistry(max_clones=3)
    first, second = registry.spawn("調査"), registry.spawn("要約", "writer")
    assert first["name"] == "ケリーエージェント2号" and first["generation"] == 2
    assert second["name"] == "ケリーエージェント3号" and second["specialist"] == "writer"
    registry.retire(first["clone_id"])
    assert registry.spawn("再生成")["name"] == "ケリーエージェント4号"


def test_clone_capacity_is_enforced():
    registry = CloneRegistry(max_clones=1)
    registry.spawn("a")
    with pytest.raises(OptimusError) as error:
        registry.spawn("b")
    assert error.value.code == "clone_capacity" and error.value.status_code == 409


def test_clone_run_uses_persona_and_cannot_self_replicate():
    agent = Mock()
    agent.run.return_value = SimpleNamespace(text="完了", status="completed", steps=1, tool_calls=0)
    registry = CloneRegistry(agent_factory=lambda: agent)
    clone = registry.spawn("市場調査")
    result = registry.run(clone["clone_id"], "競合を調べて")
    prompt = agent.run.call_args.args[0]
    assert "ケリーエージェント2号" in prompt and "市場調査" in prompt and "競合を調べて" in prompt
    assert "クローン生成・コード書き換え・デプロイは行えません" in prompt
    assert result["text"] == "完了" and result["clone"]["runs"] == 1


def test_clone_failure_is_recorded_and_unlocks():
    agent = Mock()
    agent.run.side_effect = RuntimeError("model down")
    registry = CloneRegistry(agent_factory=lambda: agent)
    clone = registry.spawn("x")
    with pytest.raises(RuntimeError):
        registry.run(clone["clone_id"], "go")
    assert registry.list()[0]["last_status"] == "failed"
    assert registry.retire(clone["clone_id"])["status"] == "retired"


def test_clone_endpoints_require_owner_and_run_with_injected_agent(client):
    http, _, _ = client
    assert http.post("/api/optimus/clones", json={"mission": "x"}).status_code == 401
    created = http.post("/api/optimus/clones", json={"mission": "翻訳"}, headers=auth())
    assert created.status_code == 201 and created.json()["name"].startswith("ケリーエージェント")
    agent = Mock()
    agent.run.return_value = SimpleNamespace(text="ok", status="completed", steps=1, tool_calls=0)
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    clone_id = created.json()["clone_id"]
    ran = http.post(f"/api/optimus/clones/{clone_id}/run", json={"prompt": "hello"}, headers=auth())
    assert ran.status_code == 200 and ran.json()["text"] == "ok"
    assert http.get("/api/optimus/clones", headers=auth()).json()["clones"][0]["runs"] == 1
    assert http.delete(f"/api/optimus/clones/{clone_id}", headers=auth()).json()["status"] == "retired"
    missing = http.delete(f"/api/optimus/clones/{clone_id}", headers=auth())
    assert missing.status_code == 404 and missing.json()["error_code"] == "clone_not_found"


def test_endpoints_fail_closed_without_owner_token(client, monkeypatch):
    http, _, _ = client
    monkeypatch.delenv("KARRY_API_TOKEN")
    assert http.get("/api/optimus/status").status_code == 503


# ----------------------------------------------------------------- evolution proposals

def test_proposal_is_preview_only_and_does_not_touch_checkout(root):
    github = Mock()
    core = OptimusCore(root, github_factory=lambda: github)
    plan = core.propose("値を更新", edit())
    assert plan["status"] == "confirmation_required" and plan["files"] == [CORE]
    assert "+VALUE = 2" in plan["diff"] and len(plan["sha256"]) == 64
    assert (root / CORE).read_text() == ORIGINAL
    github.assert_not_called()


@pytest.mark.parametrize("path", ["app.py", "src/karry_agent/optimus_core.py", "render.yaml",
                                  ".github/workflows/ci.yml", "../outside.py"])
def test_protected_or_unknown_paths_are_rejected(root, path, monkeypatch):
    monkeypatch.setenv("KARRY_OPTIMUS_PATHS", f"{CORE},{path}")
    core = OptimusCore(root)
    assert path not in optimus.evolvable_paths()
    with pytest.raises(OptimusError) as error:
        core.propose("bad", edit(path=path, old="SECURE = True\n"), paths=[path])
    assert error.value.code == "path_not_allowed"


def test_invalid_python_and_ambiguous_edits_are_rejected(root):
    core = OptimusCore(root)
    with pytest.raises(OptimusError, match="検証"):
        core.propose("broken", edit(new="VALUE = (\n"))
    with pytest.raises(OptimusError, match="検証"):
        core.propose("missing", edit(old="NOT_THERE"))


def test_generated_secret_is_rejected(root, monkeypatch):
    monkeypatch.setenv("KARRY_OPTIMUS_GITHUB_TOKEN", "ghp_secret_value_123456")
    core = OptimusCore(root)
    with pytest.raises(OptimusError):
        core.propose("leak", edit(new='VALUE = "ghp_secret_value_123456"\n'))


def test_ai_generation_budget_is_enforced(root, monkeypatch):
    monkeypatch.setenv("KARRY_OPTIMUS_MAX_GENERATIONS", "1")
    core = OptimusCore(root, generator=lambda goal, sources: {"summary": "s", "edits": edit()})
    assert core.propose("one")["summary"] == "s"
    with pytest.raises(OptimusError) as error:
        core.propose("two")
    assert error.value.code == "generation_budget"


def test_generator_failure_hides_provider_details(root):
    def broken(goal, sources):
        raise RuntimeError("provider said api_key=leak")
    with pytest.raises(OptimusError) as error:
        OptimusCore(root, generator=broken).propose("x")
    assert "leak" not in str(error.value) and error.value.code == "generation_failed"


# ----------------------------------------------------------------- confirm → PR

def test_confirm_requires_matching_sha_and_opens_pr_on_optimus_branch(client):
    http, github, _ = client
    plan = http.post("/api/optimus/evolutions", json={"goal": "改善", "edits": edit()}, headers=auth()).json()
    wrong = http.post("/api/optimus/evolutions/confirm", headers=auth(),
                      json={"approval_id": plan["approval_id"], "sha256": "0" * 64, "confirmed": True})
    assert wrong.status_code == 409 and github.opened == []
    done = http.post("/api/optimus/evolutions/confirm", headers=auth(),
                     json={"approval_id": plan["approval_id"], "sha256": plan["sha256"], "confirmed": True})
    assert done.status_code == 200 and done.json()["status"] == "pr_created"
    branch, _, body, changes, originals = github.opened[0]
    assert branch.startswith("optimus/") and changes == {CORE: "VALUE = 2\n"} and originals == {CORE: ORIGINAL}
    assert plan["sha256"] in body
    again = http.post("/api/optimus/evolutions/confirm", headers=auth(),
                      json={"approval_id": plan["approval_id"], "sha256": plan["sha256"], "confirmed": True})
    assert again.status_code == 404


def test_ai_generated_proposal_via_endpoint(client):
    http, _, _ = client
    plan = http.post("/api/optimus/evolutions", json={"goal": "AIで改善"}, headers=auth()).json()
    assert plan["summary"] == "生成された改善" and "+VALUE = 3" in plan["diff"]


def test_cancel_never_calls_github(root):
    factory = Mock()
    core = OptimusCore(root, github_factory=factory)
    plan = core.propose("x", edit())
    assert core.confirm(plan["approval_id"], plan["sha256"], False)["status"] == "cancelled"
    factory.assert_not_called()


def test_expired_proposal_cannot_be_confirmed(root):
    now = [0.0]
    core = OptimusCore(root, github_factory=Mock(), clock=lambda: now[0])
    plan = core.propose("x", edit())
    now[0] = optimus.APPROVAL_TTL + 1
    with pytest.raises(OptimusError) as error:
        core.confirm(plan["approval_id"], plan["sha256"], True)
    assert error.value.code == "approval_not_found"


def test_missing_github_token_is_503(root):
    core = OptimusCore(root)
    plan = core.propose("x", edit())
    with pytest.raises(OptimusError) as error:
        core.confirm(plan["approval_id"], plan["sha256"], True)
    assert error.value.status_code == 503 and error.value.code == "github_not_configured"


# ----------------------------------------------------------------- deploy (merge)

def deploy(http, n=7):
    return http.post(f"/api/optimus/evolutions/{n}/deploy", json={"confirmed": True}, headers=auth())


def test_deploy_requires_auto_merge_opt_in(client):
    http, github, _ = client
    response = deploy(http)
    assert response.status_code == 403 and response.json()["error_code"] == "auto_merge_disabled"
    assert github.merged == []


def test_deploy_merges_only_after_all_checks_pass(client, monkeypatch):
    http, github, _ = client
    monkeypatch.setenv("KARRY_OPTIMUS_AUTO_MERGE", "1")
    github.checks = {"state": "pending", "total": 0, "pending": [], "failed": []}
    assert deploy(http).json()["error_code"] == "checks_pending"
    github.checks = {"state": "failed", "total": 1, "pending": [], "failed": ["test"]}
    assert deploy(http).json()["error_code"] == "checks_failed"
    assert github.merged == []
    github.checks = {"state": "passed", "total": 2, "pending": [], "failed": []}
    merged = deploy(http)
    assert merged.status_code == 200 and merged.json()["status"] == "merged"
    assert github.merged == [(7, "h" * 40)] and merged.json()["deploy"] == "render_auto_deploy"


@pytest.mark.parametrize("change,code", [
    ({"head": {"ref": "feature/human", "sha": "h" * 40, "repo": {"full_name": optimus.DEFAULT_REPO}}}, "not_optimus_pr"),
    ({"head": {"ref": "optimus/abc", "sha": "h" * 40, "repo": {"full_name": "attacker/fork"}}}, "not_optimus_pr"),
    ({"base": {"ref": "release"}}, "not_optimus_pr"),
    ({"state": "closed"}, "pr_not_open"),
    ({"mergeable": None}, "not_mergeable"),
])
def test_deploy_refuses_foreign_or_unready_prs(client, monkeypatch, change, code):
    http, github, _ = client
    monkeypatch.setenv("KARRY_OPTIMUS_AUTO_MERGE", "1")
    github.pr.update(change)
    assert deploy(http).json()["error_code"] == code and github.merged == []


def test_deploy_refuses_pr_that_gained_protected_files(client, monkeypatch):
    http, github, _ = client
    monkeypatch.setenv("KARRY_OPTIMUS_AUTO_MERGE", "1")
    github.files = [CORE, ".github/workflows/ci.yml"]
    assert deploy(http).json()["error_code"] == "path_not_allowed" and github.merged == []


def test_evolution_status_reports_checks(client):
    http, _, _ = client
    body = http.get("/api/optimus/evolutions/7", headers=auth()).json()
    assert body["checks"]["state"] == "passed" and body["pr_number"] == 7


def test_status_hides_token(client, monkeypatch):
    http, _, _ = client
    monkeypatch.setenv("KARRY_OPTIMUS_GITHUB_TOKEN", "ghp_very_secret_token")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
    response = http.get("/api/optimus/status", headers=auth())
    assert response.json()["github_configured"] is True and response.json()["running_commit"] == "abcdef123456"
    assert "ghp_very_secret_token" not in response.text


# ----------------------------------------------------------------- GitHub client

def gh_response(status=200, data=None):
    obj = Mock(status_code=status)
    obj.json.return_value = data if data is not None else {}
    obj.raise_for_status.side_effect = None if status < 400 else requests.HTTPError("rejected")
    return obj


def test_github_client_opens_non_draft_pr_from_new_branch():
    session = Mock()
    session.request.side_effect = [
        gh_response(data={"object": {"sha": "base"}}),
        gh_response(data={"content": base64.b64encode(ORIGINAL.encode()).decode(), "sha": "blob"}),
        gh_response(201, {}), gh_response(200, {"commit": {"sha": "c" * 40}}),
        gh_response(201, {"number": 9, "html_url": "https://github.com/o/r/pull/9"})]
    github = OptimusGitHub("o/r", "token", session=session)
    result = github.open_pull_request("optimus/x", "title", "body", {CORE: "VALUE = 2\n"}, {CORE: ORIGINAL})
    calls = session.request.call_args_list
    assert calls[2].kwargs["json"]["ref"] == "refs/heads/optimus/x"
    assert calls[3].kwargs["json"]["branch"] == "optimus/x"
    assert calls[4].kwargs["json"]["draft"] is False and calls[4].kwargs["json"]["base"] == "main"
    assert result == {"pr_number": 9, "pr_url": "https://github.com/o/r/pull/9", "branch": "optimus/x",
                      "head_sha": "c" * 40}


def test_github_check_summary_fails_closed():
    session = Mock()
    github = OptimusGitHub("o/r", "token", session=session)
    session.request.return_value = gh_response(data={"check_runs": []})
    assert github.check_summary("s")["state"] == "pending"
    session.request.return_value = gh_response(data={"check_runs": [
        {"name": "test", "status": "completed", "conclusion": "failure"}]})
    assert github.check_summary("s")["state"] == "failed"
    session.request.return_value = gh_response(data={"check_runs": [
        {"name": "test", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "skipped"}]})
    assert github.check_summary("s")["state"] == "passed"


def test_github_merge_is_sha_pinned_and_rejection_is_mapped():
    session = Mock()
    github = OptimusGitHub("o/r", "token", session=session)
    session.request.return_value = gh_response(200, {"sha": "m", "merged": True})
    github.merge(3, "headsha", "t")
    assert session.request.call_args.kwargs["json"]["sha"] == "headsha"
    session.request.return_value = gh_response(405, {})
    with pytest.raises(OptimusError) as error:
        github.merge(3, "headsha", "t")
    assert error.value.code == "merge_rejected"
