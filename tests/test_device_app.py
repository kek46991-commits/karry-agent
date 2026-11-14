import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load_device_app(monkeypatch, role="monitor", backend="https://core.example"):
    monkeypatch.setenv("KARRY_DEVICE_ROLE", role)
    monkeypatch.setenv("KARRY_BACKEND_URL", backend)
    name = f"device_app_test_{role}_{id(monkeypatch)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "device_app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("role", "page", "blocked", "title"),
    [
        ("monitor", "/monitor", "/companion", "監視用"),
        ("worker", "/companion", "/monitor", "作業用"),
    ],
)
def test_role_shell_serves_only_its_app(monkeypatch, role, page, blocked, title):
    module = load_device_app(monkeypatch, role)
    client = TestClient(module.app)
    for path in ("/", page):
        response = client.get(path)
        assert response.status_code == 200
        assert f"<title>{title} | Karry</title>" in response.text
        assert 'href="/manifest.webmanifest"' in response.text
        assert 'window.KARRY_DEVICE_SCOPE="/"' in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get(blocked).status_code == 404
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["start_url"] == manifest["scope"] == "/"
    assert client.get("/health").json() == {"status": "ok", "role": role}


@pytest.mark.parametrize(
    "backend",
    ["", "http://core.example", "https://user:secret@core.example", "file:///tmp/core"],
)
def test_backend_must_be_a_safe_https_origin(monkeypatch, backend):
    with pytest.raises(RuntimeError):
        load_device_app(monkeypatch, backend=backend)


def test_local_http_backend_is_allowed_for_development(monkeypatch):
    module = load_device_app(monkeypatch, backend="http://127.0.0.1:8000/")
    assert module.BACKEND == "http://127.0.0.1:8000"


def test_proxy_forwards_device_auth_without_exposing_other_headers(monkeypatch):
    module = load_device_app(monkeypatch, role="worker")
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            seen.update(method=method, url=url, request=kwargs)
            return httpx.Response(
                200,
                content=b'{"ok":true}',
                headers={"content-type": "application/json", "set-cookie": "karry_device=abc; HttpOnly", "x-upstream": "hidden"},
            )

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeClient)
    response = TestClient(module.app).post(
        "/api/autonomy/device/heartbeat?detail=1",
        content=b'{"visible":true}',
        headers={
            "content-type": "application/json",
            "cookie": "karry_device=abc",
            "x-karry-device": "1",
            "authorization": "Bearer must-not-forward",
        },
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://core.example/api/autonomy/device/heartbeat"
    assert seen["request"]["params"]["detail"] == "1"
    assert seen["request"]["headers"]["cookie"] == "karry_device=abc"
    assert "authorization" not in seen["request"]["headers"]
    assert response.headers["set-cookie"].startswith("karry_device=abc")
    assert "x-upstream" not in response.headers


def test_proxy_rejects_oversized_requests_before_contacting_backend(monkeypatch):
    module = load_device_app(monkeypatch, role="worker")
    response = TestClient(module.app).put(
        "/api/autonomy/device/frame",
        content=b"x" * (module.MAX_PROXY_BODY + 1),
        headers={"x-karry-device": "1"},
    )
    assert response.status_code == 413
