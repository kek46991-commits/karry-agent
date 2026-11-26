import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jimmy_core.app import create_app
from jimmy_core.config import load_settings
from jimmy_core.dispatch import callback_token

TOKEN = "test-core-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_client(tmp_path: Path, transport=None, **extra_env):
    env = {"JIMMY_CORE_API_TOKEN": TOKEN, "JIMMY_CORE_DATA_DIR": str(tmp_path),
           "JIMMY_CORE_PUBLIC_URL": "https://core.example"} | extra_env
    return TestClient(create_app(load_settings(env), transport=transport))


@pytest.fixture
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def test_health_is_public_and_reports_storage(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["storage"] == "file" and body["auth_configured"] is True


def test_fails_closed_without_configured_token(tmp_path):
    with TestClient(create_app(load_settings({"JIMMY_CORE_DATA_DIR": str(tmp_path)}))) as c:
        assert c.get("/state", headers=AUTH).status_code == 503


@pytest.mark.parametrize("method,path", [("get", "/state"), ("post", "/memory"), ("post", "/think"),
                                         ("post", "/dispatch"), ("get", "/memory")])
def test_endpoints_require_bearer_token(client, method, path):
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_memory_roundtrip_and_state(client):
    r = client.post("/memory", headers=AUTH, json={"kind": "conversation", "content": "おはよう"})
    assert r.status_code == 201 and r.json()["id"].startswith("mem_")
    client.post("/memory", headers=AUTH, json={"kind": "decision", "content": "集客はGoogleショッピングから始める"})
    client.post("/memory", headers=AUTH, json={"kind": "task", "content": "MC登録",
                                                "task": {"id": "mc-setup", "title": "Merchant Center 登録",
                                                         "status": "in_progress"}})
    state = client.get("/state", headers=AUTH).json()
    assert state["user"]["name"] == "けいちゃん"
    assert [t["id"] for t in state["current_tasks"]] == ["mc-setup"]
    assert state["recent_decisions"][0]["content"].startswith("集客")
    assert state["recent_conversation"][0]["content"] == "おはよう"
    client.post("/memory", headers=AUTH, json={"kind": "task", "content": "完了",
                                                "task": {"id": "mc-setup", "status": "done"}})
    state = client.get("/state", headers=AUTH).json()
    assert state["current_tasks"] == [] and state["finished_tasks"][0]["title"] == "Merchant Center 登録"
    items = client.get("/memory?kind=decision", headers=AUTH).json()["items"]
    assert len(items) == 1


def test_memory_persists_across_restart(tmp_path):
    with make_client(tmp_path) as c:
        c.post("/memory", headers=AUTH, json={"kind": "note", "content": "覚えておいて"})
    with make_client(tmp_path) as c:
        assert c.get("/memory", headers=AUTH).json()["items"][0]["content"] == "覚えておいて"


def test_memory_rejects_bad_input(client):
    assert client.post("/memory", headers=AUTH, json={"kind": "nope", "content": "x"}).status_code == 422
    assert client.post("/memory", headers=AUTH, json={"content": ""}).status_code == 422
    big = {"content": "x", "metadata": {"blob": "a" * 70_000}}
    assert client.post("/memory", headers=AUTH, json=big).status_code == 422


def test_think_low_risk_proceeds_and_is_remembered(client):
    body = client.post("/think", headers=AUTH, json={"situation": "今日の作業の優先順位を整理したい"}).json()
    assert body["risk"]["level"] == "low" and body["decision"]["action"] == "proceed"
    assert body["engine"] == "rules" and body["elapsed_ms"] < 100
    state = client.get("/state", headers=AUTH).json()
    assert state["last_thought"]["id"] == body["memory_id"]


def test_think_high_risk_requires_confirmation(client):
    body = client.post("/think", headers=AUTH, json={"situation": "本番のデータベースを削除して作り直す"}).json()
    assert body["risk"]["level"] == "high"
    assert {f["category"] for f in body["risk"]["findings"]} == {"destructive", "production"}
    assert body["decision"]["action"] == "confirm_with_user"


def test_think_critical_risk_halts(client):
    body = client.post("/think", headers=AUTH,
                       json={"situation": "Stripeで顧客に請求して、終わったら個人情報を削除"}).json()
    assert body["risk"]["level"] == "critical" and body["decision"]["action"] == "halt"


def test_think_routes_to_field_agent(client):
    body = client.post("/think", headers=AUTH, json={"situation": "インタビュー動画にテロップを入れたい"}).json()
    assert body["decision"]["action"] == "dispatch" and body["decision"]["agent"] == "video"
    assert body["decision"]["agent_configured"] is False


def test_think_falls_back_when_llm_fails(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with make_client(tmp_path, transport, GEMINI_API_KEY="fake") as c:
        body = c.post("/think", headers=AUTH, json={"situation": "予定を整理"}).json()
    assert body["engine"] == "rules" and body["llm_error"] == "HTTPStatusError"


def test_think_llm_can_raise_but_not_lower_risk(tmp_path):
    reply = {"summary": "危ない", "risk_level": "critical", "next_steps": ["止める"]}

    def handler(request):
        assert request.headers["x-goog-api-key"] == "fake"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]})

    with make_client(tmp_path, httpx.MockTransport(handler), GEMINI_API_KEY="fake") as c:
        body = c.post("/think", headers=AUTH, json={"situation": "予定を整理"}).json()
    assert body["engine"] == "rules+gemini" and body["risk"]["level"] == "critical"
    assert body["decision"]["action"] == "halt" and body["analysis"]["llm_summary"] == "危ない"


def agent_env():
    return {"JIMMY_AGENT_VIDEO_URL": "https://video.example/jobs", "JIMMY_AGENT_VIDEO_TOKEN": "video-secret",
            "JIMMY_AGENT_KILLSWITCH_URL": "https://kill.example/stop"}


def test_dispatch_and_callback_flow(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"accepted": True})

    with make_client(tmp_path, httpx.MockTransport(handler), **agent_env()) as c:
        r = c.post("/dispatch", headers=AUTH, json={"agent": "video", "action": "edit", "payload": {"len": 60}})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        sent = json.loads(seen[0].content)
        assert seen[0].headers["authorization"] == "Bearer video-secret"
        assert sent["callback_url"] == "https://core.example/webhook/callback"
        assert sent["payload"] == {"len": 60}
        assert c.get(f"/dispatch/{job_id}", headers=AUTH).json()["status"] == "dispatched"

        bad = c.post("/webhook/callback", headers={"X-Jimmy-Callback-Token": "nope"},
                     json={"job_id": job_id, "status": "completed"})
        assert bad.status_code == 401
        hdr = {"X-Jimmy-Callback-Token": sent["callback_token"]}
        assert c.post("/webhook/callback", headers=hdr, json={"job_id": job_id, "status": "progress"}).json()[
            "status"] == "running"
        done = c.post("/webhook/callback", headers=hdr,
                      json={"job_id": job_id, "status": "completed", "result": {"url": "https://x/y.mp4"}})
        assert done.json() == {"job_id": job_id, "status": "completed"}
        again = c.post("/webhook/callback", headers=hdr, json={"job_id": job_id, "status": "completed"})
        assert again.json()["duplicate"] is True
        assert c.post("/webhook/callback", headers=hdr, json={"job_id": job_id, "status": "failed"}).status_code == 409
        job = c.get(f"/dispatch/{job_id}", headers=AUTH).json()
        assert job["result"] == {"url": "https://x/y.mp4"}
        state = c.get("/state", headers=AUTH).json()
        assert state["active_jobs"] == [] and state["recent_jobs"][0]["id"] == job_id
        assert "video-secret" not in json.dumps(state)


def test_dispatch_marks_unreachable_agent_failed(tmp_path):
    def handler(request):
        raise httpx.ConnectError("down")

    with make_client(tmp_path, httpx.MockTransport(handler), **agent_env()) as c:
        job_id = c.post("/dispatch", headers=AUTH, json={"agent": "video", "action": "edit"}).json()["job_id"]
        job = c.get(f"/dispatch/{job_id}", headers=AUTH).json()
    assert job["status"] == "failed" and "could not reach" in job["error"]


def test_dispatch_unknown_agent_404(client):
    assert client.post("/dispatch", headers=AUTH, json={"agent": "shop", "action": "x"}).status_code == 404


def test_killswitch_blocks_dispatch_except_killswitch_agent(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with make_client(tmp_path, transport, **agent_env()) as c:
        assert c.post("/killswitch", headers=AUTH, json={"engaged": True, "reason": "test"}).json()["engaged"]
        assert c.post("/dispatch", headers=AUTH, json={"agent": "video", "action": "edit"}).status_code == 423
        assert c.post("/dispatch", headers=AUTH, json={"agent": "killswitch", "action": "stop"}).status_code == 202
        think = c.post("/think", headers=AUTH, json={"situation": "予定を整理"}).json()
        assert think["decision"]["action"] == "halt" and think["decision"]["agent"] == "killswitch"


def test_agent_urls_must_be_https():
    with pytest.raises(ValueError):
        load_settings({"JIMMY_AGENT_SHOP_URL": "http://shop.example"})
    assert "shop" in load_settings({"JIMMY_AGENT_SHOP_URL": "http://localhost:9000",
                                    "JIMMY_CORE_ALLOW_HTTP_AGENTS": "1"}).agents


def test_callback_token_is_job_specific():
    s = load_settings({"JIMMY_CORE_API_TOKEN": TOKEN})
    assert callback_token(s, "job_000001") != callback_token(s, "job_000002")


def test_core_does_not_import_field_modules():
    root = Path(__file__).resolve().parents[1] / "jimmy_core"
    source = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    assert "karry_agent" not in source
