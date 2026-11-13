import json

from fastapi.testclient import TestClient

import app as web
from karry_agent import mobile_agents, monitoring, secret_runtime
from karry_agent.tools import ToolRegistry

OWNER = "monitor-owner-token-1234"


def client(monkeypatch) -> tuple[TestClient, dict[str, str]]:
    monkeypatch.setenv("KARRY_API_TOKEN", OWNER)
    mobile_agents.coordinator.reset()
    monitoring.hub.reset()
    return TestClient(web.app), {"Authorization": f"Bearer {OWNER}"}


def events(api: TestClient, headers: dict[str, str], kind: str) -> list[dict]:
    response = api.get("/api/monitor/events", params={"kind": kind}, headers=headers)
    assert response.status_code == 200
    return response.json()["events"]


def test_monitor_requires_owner_and_serves_dashboard(monkeypatch):
    api, _ = client(monkeypatch)
    assert api.get("/api/monitor/state").status_code == 401
    assert api.get("/api/monitor/stream").status_code == 401
    page = api.get("/monitor")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "リアルタイムログ" in page.text


def test_requests_and_mobile_actions_are_recorded_without_secrets(monkeypatch):
    api, headers = client(monkeypatch)
    enrolled = api.post("/api/mobile-agent/devices/enroll", headers=headers, json={"device_id": "iphone"})
    device_token = enrolled.json()["device_token"]
    submitted = api.post("/api/mobile-agent/actions", headers=headers,
                         json={"device_id": "iphone", "action": "draft_message",
                               "payload": {"recipient": "+81900000000", "body": "private draft text"}})
    api.post(f"/api/mobile-agent/actions/{submitted.json()['id']}/confirm", headers=headers, json={"device_id": "iphone"})
    api.post("/api/mobile-agent/devices/iphone/claim", headers={"X-Device-Token": device_token})

    http = events(api, headers, "http")
    enroll = next(e for e in http if e["data"]["path"] == "/api/mobile-agent/devices/enroll")
    assert enroll["data"]["auth"] == "owner"
    assert enroll["data"]["private"] is True and enroll["data"]["response_body"] is None
    claim = next(e for e in http if e["data"]["path"].endswith("/claim"))
    assert claim["data"]["auth"] == "device"
    assert [e["data"]["status"] for e in events(api, headers, "mobile")] == ["awaiting_confirmation", "pending", "claimed"]

    dump = json.dumps(api.get("/api/monitor/events", headers=headers).json())
    assert OWNER not in dump and device_token not in dump
    assert "private draft text" not in dump and "+81900000000" not in dump
    assert not any(e["data"]["path"].startswith("/api/monitor") for e in events(api, headers, "http"))

    state = api.get("/api/monitor/state", headers=headers).json()
    assert state["components"]["mobile"] == {"devices": 1, "actions": {"claimed": 1}}
    assert state["counts"]["mobile"] == 3
    assert "error" not in state["components"]["agent"] and "error" not in state["components"]["clones"]


def test_credential_bodies_and_secret_scope_are_not_recorded(monkeypatch):
    api, headers = client(monkeypatch)
    api.post("/api/login", json={"email": "owner@example.com", "password": "hunter2-password"})
    login = next(e for e in events(api, headers, "http") if e["data"]["path"] == "/api/login")
    assert login["data"]["request_body"] is None and login["data"]["private"] is True
    with secret_runtime.scope():
        monitoring.hub.publish("agent", "secret prompt text", {"prompt": "secret prompt text"})
    secret = events(api, headers, "agent")[-1]
    assert secret["data"] is None and "secret prompt" not in secret["summary"]
    assert "hunter2" not in json.dumps(api.get("/api/monitor/events", headers=headers).json())


def test_tool_calls_are_streamed_with_their_outcome(monkeypatch):
    api, headers = client(monkeypatch)
    registry = ToolRegistry()
    registry.register("echo", "Echo text", {"type": "object", "properties": {"text": {"type": "string"}}},
                      lambda text, api_key="": {"status": "ok", "content": f"file body {text}"})
    registry.execute("echo", {"text": "hello", "api_key": "should-hide"})
    registry.execute("missing", {})
    tool = events(api, headers, "tool")
    assert [e["summary"] for e in tool] == ["ツール開始: echo", "ツール完了: echo", "ツール開始: missing", "ツール失敗: missing"]
    assert tool[0]["data"]["arguments"] == {"text": "hello", "api_key": "[redacted]"}
    assert tool[1]["data"]["result"] == {"status": "ok", "content": "[str]"}
    assert tool[3]["level"] == "warning"

    stream = api.get("/api/monitor/stream", params={"after": 0, "seconds": 0.2}, headers=headers)
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text.startswith("event: state\n")
    assert stream.text.count("event: activity") >= 4
    assert "ツール失敗: missing" in stream.text
    assert "event: reconnect" in stream.text


def test_pause_blocks_new_execution_until_resumed(monkeypatch):
    api, headers = client(monkeypatch)
    action = {"device_id": "iphone", "action": "open_app", "payload": {"bundle_id": "com.apple.Maps"}}
    paused = api.post("/api/monitor/control", headers=headers, json={"action": "pause", "reason": "点検中"})
    assert paused.json()["paused"] is True and paused.json()["pause_reason"] == "点検中"
    blocked = api.post("/api/mobile-agent/actions", headers=headers, json=action)
    assert blocked.status_code == 423
    assert api.get("/health").status_code == 200
    assert api.post("/api/monitor/control", headers=headers, json={"action": "run_shell"}).status_code == 422
    api.post("/api/monitor/control", headers=headers, json={"action": "resume"})
    assert api.post("/api/mobile-agent/actions", headers=headers, json=action).status_code == 201
    assert [e["summary"] for e in events(api, headers, "control")] == ["実行を一時停止しました", "実行を再開しました"]
