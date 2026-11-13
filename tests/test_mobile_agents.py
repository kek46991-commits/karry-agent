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

    private_url = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "open_url",
            "payload": {"url": "https://192.168.1.1/admin"},
        },
    )
    assert private_url.status_code == 400

    malformed_url = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "owners-iphone",
            "action": "open_url",
            "payload": {"url": "https://["},
        },
    )
    assert malformed_url.status_code == 400

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


def test_android_observer_token_is_scoped_to_one_operation_device(monkeypatch):
    api, headers = client(monkeypatch)
    enroll(api, headers, "operation-iphone")
    observer = api.post(
        "/api/mobile-agent/devices/enroll",
        headers=headers,
        json={
            "device_id": "observer-android",
            "role": "observer",
            "observed_device_id": "operation-iphone",
        },
    )
    assert observer.status_code == 201
    observer_headers = {"X-Device-Token": observer.json()["device_token"]}

    action = api.post(
        "/api/mobile-agent/actions",
        headers=headers,
        json={
            "device_id": "operation-iphone",
            "action": "open_app",
            "payload": {"bundle_id": "com.apple.Preferences"},
        },
    ).json()
    visible = api.get(
        "/api/mobile-agent/observers/observer-android/devices/operation-iphone/actions",
        headers=observer_headers,
    )
    assert visible.status_code == 200
    assert visible.json()["actions"][0]["id"] == action["id"]
    assert "payload" not in visible.json()["actions"][0]

    wrong_target = api.get(
        "/api/mobile-agent/observers/observer-android/devices/other-iphone/actions",
        headers=observer_headers,
    )
    assert wrong_target.status_code == 401

    operation_token = enroll(api, headers, "second-iphone")
    wrong_role = api.get(
        "/api/mobile-agent/observers/second-iphone/devices/operation-iphone/diagnostics",
        headers=operation_token,
    )
    assert wrong_role.status_code == 401


def test_two_mobile_agent_pairs_do_not_mix_events(monkeypatch):
    api, headers = client(monkeypatch)
    pairs = (
        ("observer-android", "operation-iphone"),
        ("observer-android-3", "operation-iphone-4"),
    )
    operation_tokens: dict[str, dict[str, str]] = {}
    observer_tokens: dict[str, dict[str, str]] = {}
    event_ids: dict[str, str] = {}

    for observer_id, operation_id in pairs:
        operation_tokens[operation_id] = enroll(api, headers, operation_id)
        observer = api.post(
            "/api/mobile-agent/devices/enroll",
            headers=headers,
            json={
                "device_id": observer_id,
                "role": "observer",
                "observed_device_id": operation_id,
            },
        )
        assert observer.status_code == 201
        observer_tokens[observer_id] = {"X-Device-Token": observer.json()["device_token"]}
        action = api.post(
            "/api/mobile-agent/actions",
            headers=headers,
            json={
                "device_id": operation_id,
                "action": "choose_photo",
                "payload": {"selection_limit": 1},
            },
        )
        assert action.status_code == 201
        event_ids[operation_id] = action.json()["id"]

    for _, operation_id in pairs:
        other_operation_id = next(candidate for _, candidate in pairs if candidate != operation_id)
        claimed = api.post(
            f"/api/mobile-agent/devices/{operation_id}/claim",
            headers=operation_tokens[operation_id],
        )
        assert claimed.status_code == 200
        assert claimed.json()["action"]["id"] == event_ids[operation_id]
        denied_claim = api.post(
            f"/api/mobile-agent/devices/{other_operation_id}/claim",
            headers=operation_tokens[operation_id],
        )
        assert denied_claim.status_code == 401
        denied_report = api.post(
            f"/api/mobile-agent/actions/{event_ids[other_operation_id]}/report",
            headers=operation_tokens[operation_id],
            json={"device_id": other_operation_id, "success": True},
        )
        assert denied_report.status_code == 401
        reported = api.post(
            f"/api/mobile-agent/actions/{event_ids[operation_id]}/report",
            headers=operation_tokens[operation_id],
            json={"device_id": operation_id, "success": False, "error_code": "permission_denied"},
        )
        assert reported.status_code == 200

    for observer_id, operation_id in pairs:
        visible = api.get(
            f"/api/mobile-agent/observers/{observer_id}/devices/{operation_id}/actions",
            headers=observer_tokens[observer_id],
        )
        assert visible.status_code == 200
        assert [event["id"] for event in visible.json()["actions"]] == [event_ids[operation_id]]

        other_operation_id = next(candidate for _, candidate in pairs if candidate != operation_id)
        hidden = api.get(
            f"/api/mobile-agent/observers/{observer_id}/devices/{other_operation_id}/actions",
            headers=observer_tokens[observer_id],
        )
        assert hidden.status_code == 401
        diagnostics = api.get(
            f"/api/mobile-agent/observers/{observer_id}/devices/{operation_id}/diagnostics",
            headers=observer_tokens[observer_id],
        )
        assert diagnostics.status_code == 200
        assert [item["event_id"] for item in diagnostics.json()["diagnostics"]] == [event_ids[operation_id]]
        hidden_diagnostics = api.get(
            f"/api/mobile-agent/observers/{observer_id}/devices/{other_operation_id}/diagnostics",
            headers=observer_tokens[observer_id],
        )
        assert hidden_diagnostics.status_code == 401


def test_mobile_event_log_is_bounded():
    coordinator = mobile_agents.MobileAgentCoordinator(max_events=2, clock=lambda: 10)
    first = coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.Preferences"})
    coordinator.claim_next("iphone")
    coordinator.report(first["id"], "iphone", success=True)
    coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.MobileSMS"})
    coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.mobilesafari"})
    events = coordinator.list_events("iphone")
    assert len(events) == 2
    assert first["id"] not in {event["id"] for event in events}


def test_mobile_event_log_does_not_evict_unfinished_actions():
    coordinator = mobile_agents.MobileAgentCoordinator(max_events=2)
    first = coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.Preferences"})
    second = coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.MobileSMS"})
    try:
        coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.mobilesafari"})
        assert False, "queue should reject a third unfinished action"
    except mobile_agents.MobileAgentError as exc:
        assert "queue is full" in str(exc)
    assert {event["id"] for event in coordinator.list_events("iphone")} == {first["id"], second["id"]}


def test_claim_lease_recovers_after_response_loss():
    clock = [10.0]
    coordinator = mobile_agents.MobileAgentCoordinator(clock=lambda: clock[0])
    action = coordinator.submit("iphone", "open_app", {"bundle_id": "com.apple.Preferences"})
    assert coordinator.claim_next("iphone")["id"] == action["id"]
    assert coordinator.claim_next("iphone") is None
    clock[0] += mobile_agents.CLAIM_LEASE_SECONDS
    assert coordinator.claim_next("iphone")["id"] == action["id"]


def test_open_url_keeps_query_parameters(monkeypatch):
    monkeypatch.setattr(mobile_agents, "_resolve_public", lambda host, port, remaining: "93.184.216.34")
    coordinator = mobile_agents.MobileAgentCoordinator()
    action = coordinator.submit(
        "iphone",
        "open_url",
        {"url": "https://example.com/search?q=karry&video=123"},
    )
    assert action["payload"]["url"] == "https://example.com/search?q=karry&video=123"
