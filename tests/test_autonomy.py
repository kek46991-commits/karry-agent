import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent.autonomy import AuditLog, AutonomousRunner, JsonRunStore, MemoryRunStore, MonitorGate, RunLimits, estimate_tokens, plan_subtasks
from karry_agent.autonomy.planner import PlanError
from karry_agent.autonomy.watch import MonitorClient

TOKEN = "autonomy-test-owner-token"
MONITOR = "autonomy-test-monitor-token"


def echo(task, context):
    deps = "|".join(f"{k}={v}" for k, v in sorted(context["dependencies"].items()))
    return f"{task.id}:{task.prompt}" + (f" <- {deps}" if deps else "")


def runner(store=None, **kwargs):
    handlers = kwargs.pop("handlers", {"general": echo})
    return AutonomousRunner(store or MemoryRunStore(), handlers, **kwargs)


# ----------------------------------------------------------------- planning

def test_list_items_become_routed_subtasks_with_follow_up_dependencies():
    goal, tasks = plan_subtasks("新商品の告知を準備して\n1. 競合を調べて要約する\n2. 海の写真風の画像を生成して\n3. それをもとに告知文を書く")
    assert goal == "新商品の告知を準備して"
    assert [t.kind for t in tasks] == ["research", "image", "general"]
    assert tasks[2].depends_on == ["t2"] and tasks[0].depends_on == tasks[1].depends_on == []


def test_long_instruction_is_split_under_the_token_budget_and_deduplicated():
    text = "。".join(f"段落{i}の内容を整理する" for i in range(200)) + "。"
    _, tasks = plan_subtasks(text, budget_tokens=200)
    assert len(tasks) > 5 and all(estimate_tokens(t.prompt) <= 200 for t in tasks)
    _, dup = plan_subtasks("- 同じ作業\n- 同じ作業\n- 別の作業")
    assert [t.prompt for t in dup] == ["同じ作業", "別の作業"]


def test_too_many_subtasks_and_bad_planner_output_are_handled():
    with pytest.raises(PlanError):
        plan_subtasks("\n".join(f"- 作業{i}" for i in range(5)), max_subtasks=3)
    cyclic = lambda text, budget: [{"prompt": "a", "depends_on": [1]}, {"prompt": "b"}]
    _, tasks = plan_subtasks("- x\n- y", planner=cyclic)
    assert [t.prompt for t in tasks] == ["x", "y"]  # forward dependency rejected -> deterministic split
    good = lambda text, budget: [{"prompt": "調べる", "kind": "research"}, {"prompt": "まとめる", "depends_on": [0]}]
    _, tasks = plan_subtasks("anything", planner=good)
    assert tasks[1].depends_on == ["t1"] and tasks[0].kind == "research"


# ----------------------------------------------------------------- branching / merge / loops

def test_independent_branches_run_in_parallel_and_merge_in_order():
    barrier = threading.Barrier(2, timeout=5)

    def parallel(task, context):
        if not task.depends_on:
            barrier.wait()  # deadlocks unless both roots run at the same time
        return echo(task, context)

    r = runner(handlers={"general": parallel})
    state = r.create("- Aを書く\n- Bを書く\n- それらを踏まえて結論")
    done = r.execute(state.id)
    assert done.status == "completed" and [t.status for t in done.subtasks] == ["done"] * 3
    assert "t3:それらを踏まえて結論 <- t2=t2:Bを書く" in done.final_output
    assert done.final_output.index("## t1") < done.final_output.index("## t2") < done.final_output.index("## t3")


def test_retry_then_fail_skips_dependents_and_keeps_other_branches():
    calls = []

    def flaky(task, context):
        calls.append(task.id)
        if task.id == "t1":
            raise RuntimeError("boom")
        return echo(task, context)

    r = runner(handlers={"general": flaky}, limits=RunLimits(max_attempts=2))
    state = r.execute(r.create("- 失敗する\n- その結果を使う\n- 独立した作業").id)
    by_id = {t.id: t for t in state.subtasks}
    assert calls.count("t1") == 2 and by_id["t1"].status == "failed"
    assert by_id["t2"].status == "skipped" and by_id["t3"].status == "done"
    assert state.status == "completed" and "未完了のサブタスク" in state.final_output


def test_resume_after_lost_session_continues_and_caps_repeated_crashes(tmp_path):
    store = JsonRunStore(tmp_path)
    r = runner(store)
    state = r.create("- 一つ目\n- 二つ目")
    state.status, state.subtasks[0].status = "running", "done"
    state.subtasks[0].output = "保存済み"
    state.subtasks[1].status, state.subtasks[1].attempts = "running", 1
    store.save(state)  # simulates a worker that died mid-subtask
    resumed = runner(store).execute(state.id)
    assert resumed.status == "completed" and resumed.resumes == 1
    assert resumed.subtasks[0].output == "保存済み" and resumed.subtasks[1].attempts == 2
    assert oct(os.stat(tmp_path / f"{state.id}.json").st_mode & 0o777) == "0o600"

    looping = r.create("- 落ちる")
    looping.status, looping.subtasks[0].status, looping.subtasks[0].attempts = "running", "running", 2
    store.save(looping)
    assert runner(store).execute(looping.id).subtasks[0].status == "failed"
    capped = r.create("- 何度も再開")
    capped.status, capped.resumes = "running", 5
    store.save(capped)
    assert runner(store, limits=RunLimits(max_resumes=5)).execute(capped.id).status == "failed"


def test_empty_output_counts_as_failure_and_secrets_are_masked(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "sk-test-abcdefghijklmnopqrstu")
    r = runner(handlers={"general": lambda t, c: "" if "空" in t.prompt else "key sk-test-abcdefghijklmnopqrstu"})
    state = r.execute(r.create("- 空を返す\n- 鍵を返す").id)
    assert state.subtasks[0].status == "failed"
    assert "abcdefghijklmnop" not in state.subtasks[1].output
    assert "abcdefghijklmnop" not in json.dumps(r.audit(state.id).entries())


# ----------------------------------------------------------------- monitor

def test_policy_file_denies_and_holds_and_unreadable_file_fails_closed(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"blocked_patterns": ["rm -rf"], "blocked_kinds": ["video"]}))
    r = runner(gate=MonitorGate(policy_file=policy))
    state = r.execute(r.create("- rm -rf / を実行\n- 普通の作業").id)
    assert state.subtasks[0].status == "failed" and "拒否" in state.subtasks[0].error
    assert state.subtasks[1].status == "done"

    policy.write_text(json.dumps({"halt": True, "reason": "点検中"}))
    held = r.execute(r.create("- 作業").id)
    assert held.status == "paused" and held.hold_reason == "点検中" and held.subtasks[0].status == "blocked"
    policy.write_text("{broken")
    assert r.execute(held.id).status == "paused"
    policy.write_text("{}")
    assert r.execute(held.id).status == "completed"


def test_enforce_mode_asks_monitor_and_unreachable_monitor_holds():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self, verdict):
            self.verdict, self.posts = verdict, []

        def post(self, url, data, headers, timeout, allow_redirects):
            self.posts.append((url, json.loads(data), headers))
            if self.verdict is None:
                raise ConnectionError("down")
            return FakeResponse({"verdict": self.verdict, "reason": "ok"})

    session = FakeSession("allow")
    gate = MonitorGate("enforce", MonitorClient("https://monitor.example", "s3cret-signing", session=session))
    r = runner(gate=gate)
    assert r.execute(r.create("- 作業").id).status == "completed"
    urls = [u for u, _, _ in session.posts]
    assert "https://monitor.example/review" in urls and "https://monitor.example/events" in urls
    assert all(h["X-Karry-Signature"].startswith("sha256=") for _, _, h in session.posts)

    down = runner(gate=MonitorGate("enforce", MonitorClient("https://monitor.example", session=FakeSession(None))))
    assert down.execute(down.create("- 作業").id).status == "paused"
    with pytest.raises(ValueError):
        MonitorClient("http://monitor.example")
    assert MonitorGate("enforce").review(r.create("- x"), r.create("- x").subtasks[0]).decision == "hold"


def test_signals_pause_resume_and_stop():
    r = runner()
    state = r.create("- 一\n- 二")
    r.signal(state.id, "pause", "確認したい", actor="monitor")
    assert r.execute(state.id).status == "paused"
    r.signal(state.id, "resume")
    assert r.execute(state.id).status == "completed"
    stopped = r.create("- 止める")
    r.signal(stopped.id, "stop", "危険")
    final = r.execute(stopped.id)
    assert final.status == "stopped" and final.subtasks[0].status == "skipped"
    events = [e["event"] for e in r.audit(stopped.id).entries()]
    assert events[0] == "run_planned" and "monitor_signal" in events and events[-1] == "run_finished"


def test_audit_chain_detects_tampering_and_checks_hmac(tmp_path):
    path = tmp_path / "a.audit.jsonl"
    log = AuditLog("r1", path, secret="signing-secret-value")
    for i in range(3):
        log.append("event", {"i": i})
    assert log.verify() and all("sig" in e for e in log.entries())
    rows = path.read_text().splitlines()
    tampered = json.loads(rows[1])
    tampered["data"]["i"] = 99
    path.write_text("\n".join([rows[0], json.dumps(tampered), rows[2]]) + "\n")
    assert not log.verify()
    signed = AuditLog("r2", tmp_path / "b.audit.jsonl", secret="signing-secret-value")
    signed.append("event", {})
    assert signed.verify()
    assert not AuditLog("r2", tmp_path / "b.audit.jsonl", secret="another-secret").verify()


# ----------------------------------------------------------------- API

@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", TOKEN)
    monkeypatch.setenv("KARRY_MONITOR_TOKEN", MONITOR)
    current = runner()
    monkeypatch.setitem(web._AUTONOMY, "runner", current)
    monkeypatch.setattr(current, "start_background", lambda run_id: bool(current.execute(run_id)))
    web.app.dependency_overrides.clear()
    yield TestClient(web.app), current
    web.app.dependency_overrides.clear()


def owner():
    return {"Authorization": "Bearer " + TOKEN}


def test_api_runs_plans_and_lets_monitor_read_audit_and_signal(http):
    client, _ = http
    assert client.post("/api/autonomy/runs", json={"instruction": "- a"}).status_code == 401
    plan = client.post("/api/autonomy/runs", json={"instruction": "- 調べる\n- 書く", "dry_run": True}, headers=owner())
    assert plan.status_code == 200 and len(plan.json()["subtasks"]) == 2
    created = client.post("/api/autonomy/runs", json={"instruction": "- 調べる\n- 書く"}, headers=owner())
    assert created.status_code == 202
    run_id = created.json()["id"]
    body = client.get(f"/api/autonomy/runs/{run_id}", headers=owner()).json()
    assert body["status"] == "completed" and "# 最終出力" in body["final_output"]
    monitor = {"X-Monitor-Token": MONITOR}
    audit = client.get(f"/api/autonomy/runs/{run_id}/audit", headers=monitor).json()
    assert audit["verified"] and audit["entries"][0]["event"] == "run_planned"
    assert client.get(f"/api/autonomy/runs/{run_id}", headers=monitor).status_code == 401
    assert client.post("/api/autonomy/runs", json={"instruction": "x"}, headers=monitor).status_code == 401
    assert client.post(f"/api/autonomy/runs/{run_id}/signal", json={"action": "stop"}, headers=monitor).status_code == 409
    assert client.get("/api/autonomy/monitor", headers=owner()).json()["inbound_token"] is True


def test_media_subtasks_reuse_task_chat_and_need_a_session():
    handler = web._autonomy_media_handler
    _, tasks = plan_subtasks("- 猫の画像を生成して")
    with pytest.raises(ValueError):
        handler(tasks[0], {"session_id": None})
