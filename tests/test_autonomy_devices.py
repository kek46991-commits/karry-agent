import json
import os

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent.autonomy import AutonomousRunner, MemoryRunStore
from karry_agent.autonomy.devices import DeviceRegistry

PASSWORD = "company-enroll-password"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
W = {"X-Karry-Device": "1"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("KARRY_API_TOKEN", "owner-token-for-device-tests")
    monkeypatch.setenv("KARRY_DEVICE_ENROLL_PASSWORD", PASSWORD)
    runner = AutonomousRunner(MemoryRunStore(), {"general": lambda t, c: "ok"})
    registry = DeviceRegistry(tmp_path / "devices.json")
    monkeypatch.setitem(web._AUTONOMY, "runner", runner)
    monkeypatch.setitem(web._AUTONOMY, "devices", registry)
    from karry_agent import user_login
    user_login.clear_failures("enroll-ip:testclient")
    yield runner, registry, tmp_path


def enroll(role, name=None, password=PASSWORD):
    client = TestClient(web.app)
    response = client.post("/api/autonomy/devices/enroll", json={"role": role, "name": name or role, "password": password})
    return client, response


def test_one_step_enrollment_sets_httponly_cookie_and_stores_only_hashes(env):
    _, _, root = env
    client, response = enroll("worker", "作業スマホ")
    assert response.status_code == 200 and response.json()["device"]["role"] == "worker"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/autonomy" in cookie
    token = client.cookies.get("karry_device")
    stored = (root / "devices.json").read_text()
    assert token not in stored and oct(os.stat(root / "devices.json").st_mode & 0o777) == "0o600"
    assert client.get("/api/autonomy/device/me").json()["device"]["name"] == "作業スマホ"
    assert DeviceRegistry(root / "devices.json").authenticate(token) is not None  # survives restart


def test_enrollment_fails_closed_and_is_throttled(env, monkeypatch):
    assert enroll("monitor", password="wrong")[1].status_code == 401
    for _ in range(10):
        enroll("monitor", password="wrong")
    assert enroll("monitor")[1].status_code == 429
    monkeypatch.delenv("KARRY_DEVICE_ENROLL_PASSWORD")
    from karry_agent import user_login
    user_login.clear_failures("enroll-ip:testclient")
    assert enroll("monitor")[1].status_code == 503
    assert TestClient(web.app).post("/api/autonomy/devices/enroll", json={"role": "admin", "password": "x"}).status_code == 422


def test_worker_heartbeat_and_frames_reach_only_the_monitor(env):
    runner, _, _ = env
    worker, _ = enroll("worker")
    monitor, _ = enroll("monitor")
    run = runner.create("- 調べる\n- 書く")
    beat = worker.post("/api/autonomy/device/heartbeat", json={"visible": True, "battery": 81.4, "sharing": False, "evil": "x"}, headers=W)
    assert beat.status_code == 200 and beat.json()["runs"][0]["id"] == run.id
    assert worker.put("/api/autonomy/device/frame", content=JPEG, headers=W).status_code == 409  # not sharing yet
    worker.post("/api/autonomy/device/heartbeat", json={"sharing": True}, headers=W)
    assert worker.put("/api/autonomy/device/frame", content=b"<script>", headers=W).status_code == 400
    assert worker.put("/api/autonomy/device/frame", content=b"\xff\xd8\xff" + b"0" * 700_000, headers=W).status_code == 413
    assert worker.put("/api/autonomy/device/frame", content=JPEG, headers=W).status_code == 200

    overview = monitor.get("/api/autonomy/overview").json()
    device = next(d for d in overview["devices"] if d["role"] == "worker")
    assert device["online"] and device["sharing"] and "evil" not in device["status"]
    assert overview["runs"][0]["id"] == run.id and len(overview["runs"][0]["subtasks"]) == 2
    frame = monitor.get(f"/api/autonomy/devices/{device['id']}/frame")
    assert frame.status_code == 200 and frame.content == JPEG and frame.headers["content-type"] == "image/jpeg"
    assert worker.get("/api/autonomy/overview").status_code == 401
    assert worker.get(f"/api/autonomy/devices/{device['id']}/frame").status_code == 401
    assert worker.get(f"/api/autonomy/runs/{run.id}/audit").status_code == 401
    assert worker.post(f"/api/autonomy/runs/{run.id}/signal", json={"action": "stop"}, headers=W).status_code == 401
    assert monitor.put("/api/autonomy/device/frame", content=JPEG, headers=W).status_code == 403

    worker.post("/api/autonomy/device/heartbeat", json={"sharing": False}, headers=W)
    assert monitor.get(f"/api/autonomy/devices/{device['id']}/frame").status_code == 404
    events = [e["event"] for e in monitor.get("/api/autonomy/overview").json()["events"]]
    assert "sharing_started" in events and "sharing_stopped" in events


def test_monitor_device_controls_runs_and_needs_csrf_header(env):
    runner, _, _ = env
    monitor, _ = enroll("monitor")
    run = runner.create("- 作業")
    assert monitor.post(f"/api/autonomy/runs/{run.id}/signal", json={"action": "pause"}).status_code == 403
    paused = monitor.post(f"/api/autonomy/runs/{run.id}/signal", json={"action": "pause"}, headers=W)
    assert paused.status_code == 200 and paused.json()["control"] == "pause"
    assert monitor.get(f"/api/autonomy/runs/{run.id}/audit").json()["verified"]
    assert monitor.post("/api/autonomy/runs", json={"instruction": "x"}, headers=W).status_code == 401
    stopped = monitor.post(f"/api/autonomy/runs/{run.id}/signal", json={"action": "stop"}, headers=W).json()
    assert stopped["status"] == "stopped"
    assert any(e["data"].get("actor") == "monitor:monitor" for e in runner.audit(run.id).entries())


def test_revoked_devices_lose_access(env):
    worker, _ = enroll("worker")
    monitor, _ = enroll("monitor")
    worker_id = worker.get("/api/autonomy/device/me").json()["device"]["id"]
    assert monitor.post(f"/api/autonomy/devices/{worker_id}/revoke", headers=W).status_code == 200
    assert worker.post("/api/autonomy/device/heartbeat", json={}, headers=W).status_code == 401
    assert monitor.post("/api/autonomy/device/logout", headers=W).status_code == 200
    assert monitor.get("/api/autonomy/overview").status_code == 401


def test_device_pages_are_installable_and_locked_down(env):
    client = TestClient(web.app)
    for path, manifest in (("/monitor", "monitor"), ("/companion", "companion")):
        page = client.get(path)
        assert page.status_code == 200 and f"/static/{manifest}.webmanifest" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"] and page.headers["cache-control"] == "no-store"
        data = json.loads(client.get(f"/static/{manifest}.webmanifest").text)
        assert data["start_url"] == path and data["display"] == "standalone" and len(data["icons"]) == 2
    assert client.get("/static/icons/device-512.png").status_code == 200
    assert client.get("/device-sw.js").headers["content-type"].startswith("text/javascript")
    assert "display-capture=(self)" in client.get("/companion").headers["permissions-policy"]


def test_device_pages_are_hidden_when_experimental_is_off(env, monkeypatch):
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES", raising=False)
    client = TestClient(web.app)
    for path in ("/monitor", "/companion", "/device-sw.js", "/api/autonomy/overview"):
        assert client.get(path).status_code == 404, path
