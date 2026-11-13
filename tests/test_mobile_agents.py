from fastapi.testclient import TestClient

import app as web
from karry_agent import mobile_agents


def client(monkeypatch) -> tuple[TestClient, dict[str, str]]:
    monkeypatch.setenv("KARRY_API_TOKEN", "mobile-owner-token")
    mobile_agents.coordinator.reset()
    return TestClient(web.app), {"Authorization": "Bearer mobile-owner-token"}


def enroll(api: TestClient, headers: dict[str, str], device_id: str = "owners-iphone") -> dict[str, str]:
    response = api.post(
        "/api/mobile-agent/devices/enroll",
        headers=headers,
        json={"device_id": device_id},
    )
    assert response.status_code == 201
    return {"X-Device-Token": response.json()["device_token"]}


def test_mobile_operation_and_diagnostic_agents_are_separated(monkeypatch):
    api, headers = client(monkeypatch)
    device_headers = enroll(api, headers)
    created = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "request_location",
            "payload": {"purpose": "Show the device on the owner's dashboard"},
        },
    )
    assert created.status_code == 201
    action = created.json()
    assert action["status"] == "awaiting_confirmation"
    assert action["requires_confirmation"] is True

    empty = api.post("/api/mobile-agent/devices/owners-iphone/claim", headers=device_headers)
    assert empty.json() == {"action": None}

    confirmed = api.post(
        f"/api/mobile-agent/actions/{action['id']}/confirm",
        headers=headers,
        json={"device_id": "owners-iphone"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "pending"

    claimed = api.post(
        "/api/mobile-agent/devices/owners-iphone/claim",
        headers=device_headers,
    ).json()["action"]
    assert claimed["id"] == action["id"]
    assert claimed["status"] == "claimed"

    failed = api.post(
        f"/api/mobile-agent/actions/{action['id']}/report",
        headers=device_headers,
        json={
            "device_id": "owners-iphone",
            "success": False,
            "error_code": "permission_denied",
            "output": "location permission denied; Bearer private-device-token",
        },
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["diagnostic"] == {
        "category": "permission",
        "recommendation": "request_the_required_ios_permission",
    }
    assert "private-device-token" not in failed.text

    diagnostics = api.get(
        "/api/mobile-agent/devices/owners-iphone/diagnostics",
        headers=headers,
    ).json()["diagnostics"]
    assert diagnostics == [{
        "event_id": action["id"],
        "action": "request_location",
        "diagnostic": failed.json()["diagnostic"],
        "recorded_at": failed.json()["updated_at"],
    }]


def test_picker_actions_do_not_expose_device_wide_files(monkeypatch):
    api, headers = client(monkeypatch)
    created = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "choose_photo",
            "payload": {"selection_limit": 3},
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"
    assert created.json()["uses_system_picker"] is True
    assert created.json()["requires_confirmation"] is False

    arbitrary = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "choose_file",
            "payload": {"command": "cat /private/var/mobile/**"},
        },
    )
    assert arbitrary.status_code == 400
    assert "unsupported payload fields" in arbitrary.json()["detail"]


def test_actions_reject_unbounded_or_unsafe_capabilities(monkeypatch):
    api, headers = client(monkeypatch)
    unknown = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={"device_id": "owners-iphone", "action": "read_all_messages", "payload": {}},
    )
    assert unknown.status_code == 422

    unsafe_url = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "open_url",
            "payload": {"url": "file:///private/var/mobile/Library/SMS/sms.db"},
        },
    )
    assert unsafe_url.status_code == 400
    assert "public https URL" in unsafe_url.json()["detail"]

    credentialed_url = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "open_url",
            "payload": {"url": "https://user:password@example.com/private"},
        },
    )
    assert credentialed_url.status_code == 400

    oversized = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "create_reminder",
            "payload": {"title": "x" * 1001},
        },
    )
    assert oversized.status_code == 400


def test_device_events_are_isolated_and_owner_authenticated(monkeypatch):
    api, headers = client(monkeypatch)
    device_headers = enroll(api, headers)
    action = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "open_app",
            "payload": {"bundle_id": "com.apple.Preferences"},
        },
    ).json()

    assert api.get("/api/mobile-agent/devices/owners-iphone/actions").status_code == 401
    assert api.post("/api/mobile-agent/devices/owners-iphone/claim").status_code == 401
    assert api.post(
        "/api/mobile-agent/devices/owners-iphone/claim",
        headers={"X-Device-Token": "wrong-device-token"},
    ).status_code == 401
    assert api.post(
        "/api/mobile-agent/devices/owners-iphone/claim",
        headers=device_headers,
    ).status_code == 200
    other = api.post(
        f"/api/mobile-agent/actions/{action['id']}/confirm",
        headers=headers,
        json={"device_id": "other-iphone"},
    )
    assert other.status_code == 400
    assert "not found" in other.json()["detail"]
    assert api.get(
        "/api/mobile-agent/devices/other-iphone/actions",
        headers=headers,
    ).json() == {"actions": []}


def test_mobile_event_log_is_bounded():
    coordinator = mobile_agents.MobileAgentCoordinator(max_events=2, clock=lambda: 10)
    first = coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.Preferences"})
    coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.MobileSMS"})
    coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.mobilesafari"})
    events = coordinator.list_events("iphone")
    assert len(events) == 2
    assert first["id"] not in {event["id"] for event in events}


def test_operator_web_app_is_installable_without_owner_auth(monkeypatch):
    api, _ = client(monkeypatch)
    page = api.get("/operator")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert '<link rel="manifest" href="/operator.webmanifest">' in page.text
    manifest = api.get("/operator.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    body = manifest.json()
    assert (body["start_url"], body["display"]) == ("/operator", "standalone")
    for icon in body["icons"]:
        assert api.get(icon["src"]).headers["content-type"] == "image/png"
