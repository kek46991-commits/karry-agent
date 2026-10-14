import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import app as web
from karry_agent import instant_website as sites
from karry_agent.core import AgentCore
from karry_agent.tools import build_default_registry

HTML = '<!doctype html><html lang="ja"><head><title>Demo</title></head><body><h1>Preview</h1><script>console.log("demo")</script></body></html>'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KARRY_WEBSITE_STORAGE", "local")
    monkeypatch.setattr(sites, "INSTANT_DIR", tmp_path / "instant")
    monkeypatch.setattr(sites, "_PENDING", {})
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    for name in ("KARRY_API_TOKEN", "PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT", "GITHUB_TOKEN", "GITHUB_PAT"):
        monkeypatch.delenv(name, raising=False)


def test_direct_function_writes_and_returns_correct_url_without_github():
    result = sites.deploy_instant_website(HTML)
    assert result["success"] and result["status"] == "published"
    assert result["public_url"] == "https://example.onrender.com/static/instant/" + result["filename"]
    assert (sites.INSTANT_DIR / result["filename"]).read_text() == HTML
    assert result["sha256"] == hashlib.sha256(HTML.encode()).hexdigest()
    assert result["persistence"] == "ephemeral" and "再起動" in result["warning"]
    assert "file_path" not in result


def test_default_url_no_new_user_configuration(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL")
    result = sites.deploy_instant_website(HTML)
    assert result["public_url"].startswith("https://karry-agent-1.onrender.com/static/instant/")


@pytest.mark.parametrize("filename", ["../../index.html", "/tmp/file.html", "日本語.html", "..", ".env", "index.html"])
def test_filename_sanitized_into_isolated_unique_path(filename):
    result = sites.deploy_instant_website(HTML, filename)
    assert result["success"] and sites.public_artifact_path(result["filename"]) is not None
    assert "/" not in result["filename"] and result["filename"].endswith(".html")
    assert (sites.INSTANT_DIR / result["filename"]).parent == sites.INSTANT_DIR


def test_same_filename_never_overwrites_existing_page():
    first = sites.deploy_instant_website(HTML, "index.html")
    second = sites.deploy_instant_website("<h1>Second</h1>", "index.html")
    assert first["filename"] != second["filename"]
    assert (sites.INSTANT_DIR / first["filename"]).read_text() == HTML


@pytest.mark.parametrize("content", ["", "  ", "x" * (sites.MAX_HTML_BYTES + 1), "日" * sites.MAX_HTML_BYTES, "\x00test"])
def test_bad_html_rejected_without_file(content):
    result = sites.deploy_instant_website(content)
    assert not result["success"] and result["status"] == "error"
    assert not sites.INSTANT_DIR.exists()


@pytest.mark.parametrize("key", ["KARRY_API_TOKEN", "GITHUB_TOKEN", "PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT"])
def test_private_values_cannot_be_published(key, monkeypatch):
    value = "SyntheticPrivateValue"
    monkeypatch.setenv(key, value)
    result = sites.deploy_instant_website("<p>" + value + "</p>")
    assert not result["success"] and value not in str(result)
    assert not sites.INSTANT_DIR.exists()


@pytest.mark.parametrize("base", ["file:///tmp", "https://user:pass@example.com", "https://example.com/other", "https://example.com?token=x"])
def test_invalid_url_setting_rejected_without_write(base, monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", base)
    result = sites.deploy_instant_website(HTML)
    assert result["status"] == "error" and not sites.INSTANT_DIR.exists()


def test_prepare_only_returns_exact_payload_and_no_public_file():
    result = sites.prepare_instant_website(HTML, "example.html")
    assert result["status"] == "needs_user_action" and result["confirmation_required"]
    assert result["preview"]["html_content"] == HTML
    assert result["preview"]["sha256"] == hashlib.sha256(HTML.encode()).hexdigest()
    assert not sites.INSTANT_DIR.exists()


def test_confirm_publishes_reviewed_snapshot_not_modified_caller_file():
    pending = sites.prepare_instant_website(HTML)
    result = sites.confirm_instant_website(pending["approval_id"], pending["preview"]["sha256"], True)
    assert result["success"] and result["filename"] == pending["preview"]["filename"]
    assert (sites.INSTANT_DIR / result["filename"]).read_text() == HTML
    assert sites.confirm_instant_website(pending["approval_id"], pending["preview"]["sha256"], True)["status"] == "error"


def test_wrong_hash_never_publishes():
    pending = sites.prepare_instant_website(HTML)
    result = sites.confirm_instant_website(pending["approval_id"], "0" * 64, True)
    assert result["status"] == "error" and not sites.INSTANT_DIR.exists()


def test_expired_confirmation(monkeypatch):
    pending = sites.prepare_instant_website(HTML)
    sites._PENDING[pending["approval_id"]]["expires"] = 0
    assert sites.confirm_instant_website(pending["approval_id"], pending["preview"]["sha256"], True)["status"] == "error"
    assert not sites.INSTANT_DIR.exists()


def test_cancel_has_no_side_effect():
    pending = sites.prepare_instant_website(HTML)
    assert sites.confirm_instant_website(pending["approval_id"], pending["preview"]["sha256"], False)["status"] == "cancelled"
    assert not sites.INSTANT_DIR.exists()


def test_changed_public_destination_requires_new_review(monkeypatch):
    pending = sites.prepare_instant_website(HTML)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://other.onrender.com")
    assert sites.confirm_instant_website(pending["approval_id"], pending["preview"]["sha256"], True)["status"] == "error"
    assert not sites.INSTANT_DIR.exists()


def test_pending_and_storage_quotas(monkeypatch):
    monkeypatch.setattr(sites, "MAX_PENDING", 1)
    assert sites.prepare_instant_website(HTML)["status"] == "needs_user_action"
    assert sites.prepare_instant_website(HTML)["status"] == "error"
    monkeypatch.setattr(sites, "MAX_FILES", 1)
    assert sites.deploy_instant_website(HTML)["success"]
    assert not sites.deploy_instant_website(HTML)["success"]


def test_storage_symlink_rejected(tmp_path):
    destination = tmp_path / "private"
    destination.mkdir()
    sites.INSTANT_DIR.symlink_to(destination)
    assert not sites.deploy_instant_website(HTML)["success"]
    assert not list(destination.iterdir())


def test_model_tool_waits_and_cannot_self_approve():
    generator = Mock(return_value=SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[
        SimpleNamespace(text=None, function_call=SimpleNamespace(name="deploy_instant_website", args={"html_content": HTML}, id="call-1"))]))]))
    result = AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=generator))).run("サンプルページを作って公開して")
    assert result.status == "needs_user_action" and result.tool_calls == 1 and generator.call_count == 1
    assert not sites.INSTANT_DIR.exists()
    registry = build_default_registry()
    assert registry.execute("deploy_instant_website", {"html_content": HTML, "confirmed": True}).get("error")
    assert not sites.INSTANT_DIR.exists()


def test_direct_owner_tool_api_and_public_http_response(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    client = TestClient(web.app)
    payload = {"tool_name": "deploy_instant_website", "arguments": {"html_content": HTML, "filename": "demo.html"}}
    assert client.post("/api/agent/tools/execute", json=payload).status_code == 401
    result = client.post("/api/agent/tools/execute", headers={"Authorization": "Bearer synthetic-owner"}, json=payload).json()["result"]
    assert result["success"]
    response = client.get("/static/instant/" + result["filename"])
    assert response.status_code == 200 and response.text == HTML
    assert "sandbox allow-scripts" in response.headers["content-security-policy"]
    assert "allow-same-origin" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/").status_code == 200


def test_confirmation_api_auth_and_reviewed_content(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    pending = sites.prepare_instant_website(HTML)
    payload = {"approval_id": pending["approval_id"], "sha256": pending["preview"]["sha256"], "confirmed": True}
    client = TestClient(web.app)
    assert client.post("/api/agent/websites/confirm", json=payload).status_code == 401
    assert not sites.INSTANT_DIR.exists()
    response = client.post("/api/agent/websites/confirm", json=payload, headers={"Authorization": "Bearer synthetic-owner"})
    assert response.status_code == 200 and response.json()["success"]
    assert pending['preview']['public_url'] is None
    assert response.json()["public_url"] == pending["preview"]["planned_public_url"]


def test_fail_closed_without_owner_token():
    assert TestClient(web.app).post("/api/agent/tools/execute", json={"tool_name": "deploy_instant_website", "arguments": {"html_content": HTML}}).status_code == 503
    assert not sites.INSTANT_DIR.exists()


def test_unexpected_or_model_approval_args_rejected_by_owner_route(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    client = TestClient(web.app)
    response = client.post("/api/agent/tools/execute", headers={"Authorization": "Bearer synthetic-owner"},
                           json={"tool_name": "deploy_instant_website", "arguments": {"html_content": HTML, "confirmed": True}})
    assert response.status_code == 400 and not sites.INSTANT_DIR.exists()


def test_safe_static_fallback_headers_for_normalized_paths(monkeypatch, tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    sites.configure_static_root(root)
    try:
        result = sites.deploy_instant_website(HTML)
        app = web.FastAPI()
        app.mount("/static", web.ArtifactStaticFiles(directory=root))
        response = TestClient(app).get("/static//instant/" + result["filename"])
        assert response.status_code == 200 and response.text == HTML
        assert "sandbox allow-scripts" in response.headers["content-security-policy"]
    finally:
        sites.configure_static_root(web.STATIC_DIR)


def test_public_path_rejects_traversal_and_symlinks(tmp_path):
    assert sites.public_artifact_path("../../index.html") is None
    assert sites.public_artifact_path("index.html") is None
    result = sites.deploy_instant_website(HTML)
    path = sites.INSTANT_DIR / result["filename"]
    path.unlink()
    other = tmp_path / "outside.html"; other.write_text("private")
    path.symlink_to(other)
    assert sites.public_artifact_path(result["filename"]) is None


def test_permission_error_does_not_expose_path_or_html(monkeypatch):
    monkeypatch.setattr(Path, "mkdir", Mock(side_effect=PermissionError("private-storage-secret")))
    result = sites.deploy_instant_website(HTML)
    assert result["status"] == "error" and "private-storage-secret" not in str(result) and HTML not in str(result)


def test_compatibility_module_exports_function():
    import agent_core
    assert agent_core.deploy_instant_website is sites.deploy_instant_website


def test_fallback_cannot_serve_unmanaged_or_staging_files(tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    sites.configure_static_root(root)
    try:
        sites.INSTANT_DIR.mkdir()
        (sites.INSTANT_DIR / "unmanaged.html").write_text("private")
        (sites.INSTANT_DIR / ".pending-example.tmp").write_text("partial")
        app = web.FastAPI()
        app.mount("/static", web.ArtifactStaticFiles(directory=root))
        client = TestClient(app)
        for name in ("unmanaged.html", ".pending-example.tmp"):
            assert client.get("/static//instant/" + name).status_code == 404
            assert client.get("/static///instant/" + name).status_code == 404
    finally:
        sites.configure_static_root(web.STATIC_DIR)


def test_final_url_appears_only_after_all_html_is_written(monkeypatch):
    real_link = sites.os.link
    client = TestClient(web.app)
    def checked_link(source, target):
        assert Path(source).read_text() == HTML
        assert not Path(target).exists()
        assert client.get("/static/instant/" + Path(target).name).status_code == 404
        real_link(source, target)
    monkeypatch.setattr(sites.os, "link", checked_link)
    result = sites.deploy_instant_website(HTML)
    assert result["success"]
    response = client.get("/static/instant/" + result["filename"])
    assert response.status_code == 200 and response.text == HTML
    assert not list(sites.INSTANT_DIR.glob(".pending-*"))


def test_short_credential_is_not_an_identifier_substring(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "fake")
    assert sites.deploy_instant_website("<h1>fake-brand</h1>")["success"]
    assert not sites.deploy_instant_website("<p>Bearer fake</p>")["success"]
    assert not sites.deploy_instant_website('<input value="fake">')["success"]


def test_atomic_collision_never_overwrites_previous_artifact(monkeypatch):
    monkeypatch.setattr(sites.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    first = sites.deploy_instant_website(HTML)
    second = sites.deploy_instant_website("<h1>Not allowed overwrite</h1>")
    assert first["success"] and not second["success"]
    assert (sites.INSTANT_DIR / first["filename"]).read_text() == HTML
    assert not list(sites.INSTANT_DIR.glob(".pending-*"))
