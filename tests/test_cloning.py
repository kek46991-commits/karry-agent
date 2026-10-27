from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import cloning, cloning_api

OWNER_TOKEN = "synthetic-clone-owner"
SHA = "a" * 40


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, render_status=201):
        self.calls = []
        self.render_status = render_status

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(200, {"object": {"sha": SHA}})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/git/refs"):
            return FakeResponse(201, {"ref": "refs/heads/sandbox/experiment"})
        if "/services" in url:
            body = {"service": {"id": "srv-sandbox-123", "name": "karry-agent-sandbox-experiment",
                                 "dashboardUrl": "https://dashboard.render.com/web/srv-sandbox-123"},
                    "deployId": "dep-sandbox-123"}
            return FakeResponse(self.render_status, body)
        raise AssertionError(url)


def make_service(session=None, plan="free", source_sha=""):
    settings = cloning.CloneSettings(
        repository="kek46991-commits/karry-agent",
        github_token="github-secret-not-returned",
        base_branch="main",
        source_sha=source_sha,
        render_api_key="render-secret-not-returned",
        render_owner_id="tea-sandbox-workspace",
        render_region="oregon",
        render_plan=plan,
    )
    return cloning.CloneService(settings, session=session or FakeSession())


@pytest.fixture(autouse=True)
def configure_auth(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", OWNER_TOKEN)
    web.app.dependency_overrides.clear()
    yield
    web.app.dependency_overrides.clear()


@pytest.fixture
def client():
    return TestClient(web.app)


def test_create_branch_reads_configured_base_sha_and_creates_sandbox_ref():
    session = FakeSession()
    service = make_service(session)
    branch = service.create_branch("experiment")
    assert branch.repository == "kek46991-commits/karry-agent"
    assert branch.branch == "sandbox/experiment"
    assert branch.sha == SHA
    assert branch.url.endswith("/tree/sandbox/experiment")
    assert session.calls[0][1].endswith("/git/ref/heads/main")
    posted = session.calls[1][2]
    assert posted["json"] == {"ref": "refs/heads/sandbox/experiment", "sha": SHA}
    assert posted["headers"]["Authorization"] == "Bearer github-secret-not-returned"


def test_running_deploy_sha_is_used_for_clone_source():
    session = FakeSession()
    service = make_service(session, source_sha=SHA)
    branch = service.create_branch("deployed-state")
    assert branch.sha == SHA
    assert [call[0] for call in session.calls] == ["POST"]


def test_render_environment_variables_detect_current_clone_source(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_REPO_SLUG", "owner/karry-agent")
    monkeypatch.setenv("RENDER_GIT_BRANCH", "release")
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    monkeypatch.setenv("KARRY_CLONE_GITHUB_TOKEN", "github-test-token")
    monkeypatch.delenv("KARRY_CLONE_REPOSITORY", raising=False)
    settings = cloning.CloneSettings.from_env()
    assert settings.repository == "owner/karry-agent"
    assert settings.base_branch == "release"
    assert settings.source_sha == SHA


def test_render_service_is_minimal_free_checks_gated_and_does_not_copy_secrets():
    session = FakeSession()
    service = make_service(session)
    result = service.create_render_service("sandbox/experiment", "experiment")
    assert result["id"] == "srv-sandbox-123"
    _, url, kwargs = session.calls[-1]
    assert url == "https://api.render.com/v1/services"
    assert kwargs["headers"]["Authorization"] == "Bearer render-secret-not-returned"
    payload = kwargs["json"]
    assert payload["ownerId"] == "tea-sandbox-workspace"
    assert payload["repo"] == "https://github.com/kek46991-commits/karry-agent"
    assert payload["branch"] == "sandbox/experiment"
    assert payload["autoDeployTrigger"] == "checksPass"
    assert payload["serviceDetails"]["plan"] == "free"
    assert "envVars" not in payload
    assert "render-secret-not-returned" not in repr(payload)


@pytest.mark.parametrize("slug", ["", "Upper", "../prod", "space name", "a" * 25])
def test_branch_slug_rejects_invalid_names(slug):
    with pytest.raises(ValueError):
        make_service().create_branch(slug)


def test_missing_provider_token_is_configuration_error(monkeypatch):
    monkeypatch.setenv("KARRY_CLONE_REPOSITORY", "kek46991-commits/karry-agent")
    monkeypatch.delenv("KARRY_CLONE_GITHUB_TOKEN", raising=False)
    with pytest.raises(cloning.CloneConfigurationError):
        cloning.CloneSettings.from_env()


def test_non_free_render_plan_is_rejected(monkeypatch):
    monkeypatch.setenv("KARRY_CLONE_REPOSITORY", "kek46991-commits/karry-agent")
    monkeypatch.setenv("KARRY_CLONE_GITHUB_TOKEN", "github-test-token")
    monkeypatch.setenv("KARRY_CLONE_RENDER_PLAN", "starter")
    with pytest.raises(cloning.CloneConfigurationError):
        cloning.CloneSettings.from_env()


def test_endpoint_requires_owner_auth(client):
    response = client.post("/api/agent/clone", json={"slug": "experiment"})
    assert response.status_code == 401


def test_endpoint_fails_closed_without_owner_token(client, monkeypatch):
    monkeypatch.delenv("KARRY_API_TOKEN")
    response = client.post("/api/agent/clone", json={"slug": "experiment"},
                           headers={"X-Owner-Token": OWNER_TOKEN})
    assert response.status_code == 503


def test_endpoint_fails_closed_when_github_clone_integration_is_unconfigured(client, monkeypatch):
    monkeypatch.delenv("KARRY_CLONE_REPOSITORY", raising=False)
    monkeypatch.delenv("KARRY_CLONE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("RENDER_GIT_REPO_SLUG", raising=False)
    response = client.post("/api/agent/clone", json={"slug": "experiment"},
                           headers={"X-Owner-Token": OWNER_TOKEN})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"


def test_endpoint_creates_only_branch_by_default(client):
    session = FakeSession()
    service = make_service(session)
    web.app.dependency_overrides[cloning_api.get_clone_service] = lambda: service
    response = client.post("/api/agent/clone", json={"slug": "experiment"},
                           headers={"Authorization": f"Bearer {OWNER_TOKEN}"})
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "branch_created"
    assert response.json()["branch"] == "sandbox/experiment"
    assert response.json()["render"] is None
    assert len(session.calls) == 2


def test_render_requires_explicit_resource_creation_confirmation_before_branch(client):
    session = FakeSession()
    web.app.dependency_overrides[cloning_api.get_clone_service] = lambda: make_service(session)
    response = client.post("/api/agent/clone", json={"slug": "experiment", "provision_render": True},
                           headers={"X-Owner-Token": OWNER_TOKEN})
    assert response.status_code == 400
    assert session.calls == []


def test_confirmed_render_provision_creates_branch_and_service(client):
    session = FakeSession()
    web.app.dependency_overrides[cloning_api.get_clone_service] = lambda: make_service(session)
    response = client.post("/api/agent/clone", json={"slug": "experiment", "provision_render": True,
                           "confirm_resource_creation": True}, headers={"X-Owner-Token": OWNER_TOKEN})
    assert response.status_code == 201
    assert response.json()["status"] == "sandbox_created"
    assert response.json()["render"]["id"] == "srv-sandbox-123"
    assert len(session.calls) == 3


def test_failed_render_provision_reports_created_branch_without_leaking_secrets(client):
    session = FakeSession(render_status=500)
    web.app.dependency_overrides[cloning_api.get_clone_service] = lambda: make_service(session)
    response = client.post("/api/agent/clone", json={"slug": "experiment", "provision_render": True,
                           "confirm_resource_creation": True}, headers={"X-Owner-Token": OWNER_TOKEN})
    assert response.status_code == 502
    assert response.json()["partial_success"]["branch"] == "sandbox/experiment"
    assert "github-secret-not-returned" not in response.text
    assert "render-secret-not-returned" not in response.text


def test_endpoint_rejects_unconfigured_extra_target_fields(client):
    web.app.dependency_overrides[cloning_api.get_clone_service] = lambda: make_service()
    response = client.post("/api/agent/clone", json={"slug": "experiment", "repository": "attacker/repo"},
                           headers={"X-Owner-Token": OWNER_TOKEN})
    assert response.status_code == 422
