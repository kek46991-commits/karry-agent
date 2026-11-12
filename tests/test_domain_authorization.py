from fastapi.testclient import TestClient

import app as app_module
from karry_agent import domain_authorization


def _reset(monkeypatch):
    monkeypatch.setenv("KARRY_TASK_STORAGE", "memory")
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in (
        "_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR",
        "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS",
    ):
        monkeypatch.setattr(app_module, name, {})
    monkeypatch.setattr(domain_authorization, "_AUTHORIZATIONS", {})


def test_owner_consent_precedes_domain_control_verification(monkeypatch):
    monkeypatch.setattr(domain_authorization.secrets, "token_hex", lambda _: "a" * 32)
    record, token = domain_authorization.create("session-1", "https://Example.com/path")
    assert record.origin == "https://example.com"
    assert not record.consented
    assert not record.verified

    try:
        domain_authorization.verify(record.id, "session-1", "dns", verifier=lambda _: True)
    except domain_authorization.AuthorizationError as exc:
        assert "所有者の承認" in str(exc)
    else:
        raise AssertionError("verification unexpectedly bypassed owner consent")

    domain_authorization.approve(record.id, token)
    verified = domain_authorization.verify(record.id, "session-1", "dns", verifier=lambda _: True)
    assert verified.consented
    assert verified.verified
    assert verified.method == "dns"
    assert domain_authorization.target_is_authorized("https://example.com/private")
    assert domain_authorization.target_is_authorized(
        "https://example.com/private",
        session_id="session-1",
    )
    assert not domain_authorization.target_is_authorized(
        "https://example.com/private",
        session_id="session-2",
    )


def test_authorization_rejects_wrong_approval_link_and_session(monkeypatch):
    monkeypatch.setattr(domain_authorization, "_AUTHORIZATIONS", {})
    record, _ = domain_authorization.create("session-1", "https://example.com")
    try:
        domain_authorization.approve(record.id, "wrong-token")
    except domain_authorization.AuthorizationError as exc:
        assert "無効" in str(exc)
    else:
        raise AssertionError("wrong approval token unexpectedly succeeded")
    try:
        domain_authorization.verify(record.id, "other-session", "file", verifier=lambda _: True)
    except domain_authorization.AuthorizationError as exc:
        assert "見つかりません" in str(exc)
    else:
        raise AssertionError("cross-session verification unexpectedly succeeded")


def test_hack_domain_authorization_api_and_public_consent_page(monkeypatch):
    _reset(monkeypatch)
    client = TestClient(app_module.app)
    headers = {"Authorization": "Bearer synthetic-owner"}
    hack = client.post("/api/sessions", headers=headers, json={"title": "ハック"}).json()
    normal = client.post("/api/sessions", headers=headers, json={"title": "通常"}).json()

    denied = client.post("/api/hack/domain-authorizations", headers=headers, json={
        "session_id": normal["session_id"],
        "target_url": "https://example.com",
    })
    assert denied.status_code == 403

    created = client.post("/api/hack/domain-authorizations", headers=headers, json={
        "session_id": hack["session_id"],
        "target_url": "https://example.com/path",
    })
    assert created.status_code == 200
    payload = created.json()
    assert payload["origin"] == "https://example.com"
    assert payload["consented"] is False
    assert "approval_url" in payload

    page = client.get(payload["approval_url"])
    assert page.status_code == 200
    assert "Yes, authorize verification" in page.text

    token = payload["approval_url"].split("token=", 1)[1]
    approved = client.post(
        f"/api/hack/domain-authorizations/{payload['id']}/approve",
        data={"token": token},
    )
    assert approved.status_code == 200
    assert "Approval recorded" in approved.text

    listed = client.get(
        "/api/hack/domain-authorizations",
        headers=headers,
        params={"session_id": hack["session_id"]},
    ).json()
    assert listed["authorizations"][0]["consented"] is True


def test_hack_scope_requires_verified_remote_target():
    from karry_agent.hack_task_mode import authorize_target, scope

    prompt = "https://example.com の脆弱性を検証"
    with scope("ハック", prompt, require_verification=True):
        try:
            authorize_target("https://example.com")
        except PermissionError as exc:
            assert "所有者承認" in str(exc)
        else:
            raise AssertionError("unverified remote target unexpectedly authorized")

    with scope(
        "ハック",
        prompt,
        verified_targets=frozenset({"https://example.com"}),
        require_verification=True,
    ):
        assert authorize_target("https://example.com") == "https://example.com"
