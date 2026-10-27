import sys

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import self_correction as sc
from karry_agent.self_healing import SelfHealingEngine

AUTH = {"Authorization": "Bearer synthetic-owner"}
BROKEN = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BROKEN)
    (root / "tests" / "test_calc.py").write_text(TEST)
    (root / ".git").mkdir()
    (root / ".git" / "secret").write_text("never copied")
    return root


def edit(old, new):
    return {"summary": f"{old} -> {new}", "edits": [{"path": "calc.py", "old": old, "new": new}]}


def run_loop(repo, proposals, **kw):
    seen = []

    def generator(incident, sources):
        seen.append((incident, dict(sources)))
        return proposals.pop(0)

    loop = sc.SelfCorrectionLoop(repo, generator=generator, allowed_paths=("calc.py",),
                                 default_tests=("tests/test_calc.py",), **kw)
    job = loop.submit()
    loop.close()
    return loop.job(job["job_id"]), seen


def test_failing_check_is_fixed_in_a_copy_and_rerun(repo):
    job, seen = run_loop(repo, [edit("return a - b", "return a + b")])
    assert job["status"] == "corrected" and job["verified"] is True and job["deployed"] is False
    assert [a["result"] for a in job["attempts"]] == ["passed"]
    assert "+    return a + b" in job["diff"] and job["files"] == ["calc.py"] and len(job["patch_sha256"]) == 64
    assert (repo / "calc.py").read_text() == BROKEN
    incident, sources = seen[0]
    assert "assert" in incident["log"] and incident["check"] == ["tests/test_calc.py"] and sources == {"calc.py": BROKEN}


def test_loop_feeds_the_new_failure_back_until_it_passes(repo):
    job, seen = run_loop(repo, [edit("return a - b", "return a * b"), edit("return a * b", "return a + b")])
    assert job["status"] == "corrected" and [a["result"] for a in job["attempts"]] == ["still_failing", "passed"]
    assert seen[1][1]["calc.py"].endswith("return a * b\n") and seen[1][0]["previous_attempts"][0]["attempt"] == 1


def test_invalid_or_useless_proposals_stop_after_max_attempts(repo):
    bad = [{"edits": [{"path": "../calc.py", "old": "x", "new": "y"}]}, edit("return a - b", "return (")]
    job, _ = run_loop(repo, bad, max_attempts=2)
    assert job["status"] == "not_corrected" and job["verified"] is False
    assert [a["result"] for a in job["attempts"]] == ["proposal_rejected", "proposal_rejected"]
    assert (repo / "calc.py").read_text() == BROKEN


def test_passing_check_needs_no_model(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    job, seen = run_loop(repo, [])
    assert job["status"] == "passed_without_changes" and seen == []


@pytest.mark.parametrize("target", ["../tests/x.py", "rm -rf /", "src/app.py", "tests/a.py; ls", "-k foo"])
def test_check_command_only_accepts_pytest_paths_under_tests(target):
    with pytest.raises(sc.SelfCorrectionError):
        sc.check_targets([target])


def test_sandbox_env_drops_secrets_and_prefers_the_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "g-secret")
    monkeypatch.setenv("KARRY_API_TOKEN", "owner-secret")
    env = sc.sandbox_env(tmp_path)
    assert "GEMINI_API_KEY" not in env and "KARRY_API_TOKEN" not in env
    assert env["PYTHONPATH"].split(":")[0] == str(tmp_path / "src")


def test_runtime_incident_hook_queues_a_job_and_updates_the_incident(repo):
    engine = SelfHealingEngine()
    calls = []
    loop = sc.SelfCorrectionLoop(repo, generator=lambda i, s: edit("return a - b", "return a + b"), engine=engine,
                                 allowed_paths=("calc.py",), default_tests=("tests/test_calc.py",),
                                 runner=lambda cmd, cwd, env, t: calls.append(cmd) or (0 if "+" in (cwd / "calc.py").read_text() else 1, "E assert"))
    engine.set_patch_hook(loop.submit_incident)
    result = engine.analyze_and_heal(RuntimeError("boom"), {"source": "tool"})
    loop.close()
    assert calls[0][:3] == [sys.executable, "-m", "pytest"]
    incident = engine.error_logs[-1]
    assert incident["incident_id"] and result.get("status") in {"self_correction_queued", "corrected"}
    assert loop.jobs()[0]["status"] == "corrected" and loop.jobs()[0]["incident_id"] == incident["incident_id"]


def test_budget_limits_jobs_per_process(repo):
    loop = sc.SelfCorrectionLoop(repo, generator=lambda i, s: {}, runner=lambda *a: (0, ""), max_jobs=1,
                                 allowed_paths=("calc.py",), default_tests=("tests/test_calc.py",))
    loop.submit()
    with pytest.raises(sc.SelfCorrectionError):
        loop.submit()
    assert loop.submit_incident({"incident_id": "x"})["action"] == "self_correction_budget_exhausted"
    loop.close()


def test_configuration_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("KARRY_SELF_CORRECTION", raising=False)
    assert sc.configure_self_correction(SelfHealingEngine(), tmp_path) is None


def test_api_is_experimental_owner_only_and_reports_jobs(monkeypatch, repo):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    client = TestClient(web.app)
    monkeypatch.setattr(web, "_SELF_CORRECTION", None)
    assert client.post("/api/agent/self-correction", headers=AUTH).status_code == 503
    loop = sc.SelfCorrectionLoop(repo, generator=lambda i, s: {}, runner=lambda *a: (0, ""),
                                 allowed_paths=("calc.py",), default_tests=("tests/test_calc.py",))
    monkeypatch.setattr(web, "_SELF_CORRECTION", loop)
    assert client.post("/api/agent/self-correction").status_code == 401
    assert client.post("/api/agent/self-correction", json={"tests": ["../etc"]}, headers=AUTH).status_code == 422
    started = client.post("/api/agent/self-correction", json={"tests": ["tests/test_calc.py"]}, headers=AUTH)
    assert started.status_code == 202
    loop.close()
    job = client.get("/api/agent/self-correction/" + started.json()["job_id"], headers=AUTH).json()
    assert job["status"] == "passed_without_changes"
    assert client.get("/api/agent/self-correction", headers=AUTH).json()["jobs"][0]["job_id"] == job["job_id"]
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES")
    assert client.post("/api/agent/self-correction", headers=AUTH).status_code == 404
