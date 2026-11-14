import json
import plistlib
import threading
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from karry_agent import ota_enrollment as ota

UDID = "00008110-000A1B2C3D4E5F60"
OLD_UDID = "a" * 40


def attributes(udid=UDID, **extra):
    return plistlib.dumps({"UDID": udid, "PRODUCT": "iPhone16,2", "VERSION": "23A340", **extra})


def wrapped(plist: bytes) -> bytes:
    return b"\x30\x82\x01\x00\x06\x09*\x86H\x86\xf7\r\x01\x07\x02" + plist + b"\x00" * 40 + b"\x31\x82\x00\x10"


def signed_ipa(path, devices):
    profile = b"\x30\x82binary" + plistlib.dumps({"ProvisionedDevices": devices, "Name": "fixture"}) + b"\x00sig"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Payload/K.app/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": "com.kishi.karryagent2", "CFBundleVersion": "7", "CFBundleDisplayName": "Karry-Agent 2"}))
        archive.writestr("Payload/K.app/embedded.mobileprovision", profile)
        archive.writestr("Payload/K.app/_CodeSignature/CodeResources", "x")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_OTA_ENROLLMENT", "1")
    monkeypatch.delenv("KARRY_OTA_INVITE_CODE", raising=False)
    monkeypatch.delenv("KARRY_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("KARRY_OTA_PROFILE_SIGNING_CERT", raising=False)
    clock = {"now": 1_700_000_000.0}
    registry = ota.DeviceRegistry(str(tmp_path / "registry.json"), clock=lambda: clock["now"])
    ota_dir = tmp_path / "ota"
    ota_dir.mkdir()
    app = FastAPI()
    owner_calls = []

    def require_owner():
        owner_calls.append(1)

    app.include_router(ota.build_router(require_owner, registry, ota.SignedBuild(str(ota_dir))))
    client = TestClient(app, base_url="https://agent.example")
    client.clock, client.registry, client.ota_dir, client.owner_calls = clock, registry, ota_dir, owner_calls
    return client


def issue(api) -> tuple[str, dict]:
    response = api.get("/ota/profile")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-apple-aspen-config")
    profile = plistlib.loads(response.content)
    token = profile["PayloadContent"]["URL"].rsplit("/", 1)[1]
    return token, profile


def test_disabled_by_default_returns_404(tmp_path, monkeypatch):
    monkeypatch.delenv("KARRY_OTA_ENROLLMENT", raising=False)
    app = FastAPI()
    app.include_router(ota.build_router(lambda: None, ota.DeviceRegistry("memory"), ota.SignedBuild(None)))
    client = TestClient(app)
    assert client.get("/ota/").status_code == 404
    assert client.post("/api/register-udid/" + "a" * 24, content=attributes()).status_code == 404


def test_profile_requests_only_udid_product_version_and_points_at_token_url(api):
    page = api.get("/ota/", headers={"User-Agent": IOS_SAFARI})
    assert page.status_code == 200 and "デバイスを登録して進む" in page.text and 'action="/ota/profile"' in page.text
    token, profile = issue(api)
    assert profile["PayloadType"] == "Profile Service"
    assert profile["PayloadContent"]["DeviceAttributes"] == ["UDID", "PRODUCT", "VERSION"]
    assert profile["PayloadContent"]["URL"] == f"https://agent.example/api/register-udid/{token}"
    assert "IMEI" not in profile["PayloadContent"]["DeviceAttributes"]
    assert "attachment" in api.get("/ota/profile").headers["content-disposition"]


IOS_SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"


@pytest.mark.parametrize(("agent", "kind"), [
    (IOS_SAFARI, "ios_safari"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 CriOS/130.0 Mobile/15E148 Safari/604.1", "ios_other"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Line/14.0", "ios_other"),
    ("Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36", "android"),
    ("Mozilla/5.0 (Windows NT 10.0) Chrome/130.0", "other"), ("", "other"),
])
def test_browser_classification(agent, kind):
    assert ota.classify_browser(agent) == kind


def test_register_page_explains_non_safari_and_allows_force(api):
    android = api.get("/ota/", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 15) Chrome/130.0 Mobile Safari/537.36"})
    assert "Androidです" in android.text and 'action="/ota/profile"' not in android.text
    forced = api.get("/ota/?force=1", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 15) Chrome/130.0 Mobile Safari/537.36"})
    assert 'action="/ota/profile"' in forced.text
    safari = api.get("/ota/", headers={"User-Agent": IOS_SAFARI})
    assert 'action="/ota/profile"' in safari.text and 'id="after"' in safari.text


def test_public_base_url_override_and_forwarded_proto(api, monkeypatch):
    monkeypatch.setenv("KARRY_PUBLIC_BASE_URL", "https://ota.example/")
    _, profile = issue(api)
    assert profile["PayloadContent"]["URL"].startswith("https://ota.example/api/register-udid/")


def test_pkcs7_wrapped_post_registers_and_redirects_to_pending_status(api):
    token, _ = issue(api)
    response = api.post(f"/api/register-udid/{token}", content=wrapped(attributes()), follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == f"/ota/status/{token}"
    status = api.get(f"/ota/status/{token}")
    assert status.status_code == 200
    assert "登録完了" in status.text and "ビルド待ち" in status.text
    assert UDID not in status.text and "00008110…5F60" in status.text
    assert "itms-services" not in status.text
    stored = json.loads((api.registry._path).read_text())
    assert stored["devices"][UDID]["product"] == "iPhone16,2"


def test_registered_device_covered_by_signed_build_auto_opens_itms_services(api):
    signed_ipa(api.ota_dir / "KarryOperator.ipa", [UDID.lower(), "00008030-0000000000000000"])
    token, _ = issue(api)
    api.post(f"/api/register-udid/{token}", content=attributes(), follow_redirects=False)
    status = api.get(f"/ota/status/{token}")
    link = "itms-services://?action=download-manifest&amp;url=https%3A%2F%2Fagent.example%2Fota%2Fmanifest.plist"
    assert link in status.text and 'http-equiv="refresh"' in status.text
    manifest = plistlib.loads(api.get("/ota/manifest.plist").content)
    item = manifest["items"][0]
    assert item["assets"][0]["url"] == "https://agent.example/ota/app.ipa"
    assert item["metadata"] == {"bundle-identifier": "com.kishi.karryagent2", "bundle-version": "7",
                                "kind": "software", "title": "Karry-Agent 2"}
    assert api.get("/ota/app.ipa").status_code == 200


def test_provisioned_devices_json_overrides_embedded_profile(api):
    signed_ipa(api.ota_dir / "K.ipa", [])
    (api.ota_dir / "provisioned_devices.json").write_text(json.dumps([UDID]))
    token, _ = issue(api)
    api.post(f"/api/register-udid/{token}", content=attributes(), follow_redirects=False)
    assert "itms-services" in api.get(f"/ota/status/{token}").text


def test_token_is_single_use_and_expires(api):
    token, _ = issue(api)
    api.post(f"/api/register-udid/{token}", content=attributes(), follow_redirects=False)
    second = api.post(f"/api/register-udid/{token}", content=attributes(udid=OLD_UDID), follow_redirects=False)
    assert second.status_code == 301
    assert OLD_UDID.upper() not in api.registry.devices()
    stale, _ = issue(api)
    api.clock["now"] += ota.TOKEN_TTL_SECONDS + 1
    expired = api.post(f"/api/register-udid/{stale}", content=attributes(udid=OLD_UDID), follow_redirects=False)
    assert expired.headers["location"] == f"/ota/status/{stale}"
    assert api.get(f"/ota/status/{stale}").status_code == 410
    assert api.get("/ota/status/not-a-token").status_code == 410
    assert api.post("/api/register-udid/bad!token", content=attributes(), follow_redirects=False).headers["location"] == "/ota/status/invalid"


@pytest.mark.parametrize("body", [b"not a plist", attributes(udid="12345"), attributes(udid="<script>"),
                                  plistlib.dumps(["UDID"]), b"<?xml version='1.0'?><plist version='1.0'><dict><key>UDID</key>"
                                  b"<string>00008110-000A1B2C3D4E5F60</string></dict></plist>" * 2000])
def test_invalid_bodies_register_nothing_and_show_error_page(api, body):
    token, _ = issue(api)
    response = api.post(f"/api/register-udid/{token}", content=body, follow_redirects=False)
    assert response.status_code == 301
    assert api.registry.devices() == {}
    status = api.get(f"/ota/status/{token}")
    assert status.status_code in (400, 202)
    assert "<script>" not in status.text


def test_unused_token_shows_waiting_page(api):
    token, _ = issue(api)
    assert api.get(f"/ota/status/{token}").status_code == 202


def test_owner_endpoints_use_owner_auth_and_report_provisioning(api):
    signed_ipa(api.ota_dir / "K.ipa", [UDID])
    token, _ = issue(api)
    api.post(f"/api/register-udid/{token}", content=attributes(), follow_redirects=False)
    other, _ = issue(api)
    api.post(f"/api/register-udid/{other}", content=attributes(udid=OLD_UDID), follow_redirects=False)
    listing = api.get("/api/ota/devices").json()
    assert api.owner_calls
    assert [(d["udid"], d["provisioned"]) for d in listing["devices"]] == [(UDID, True), (OLD_UDID.upper(), False)]
    assert listing["signed_build"]["provisioned_count"] == 1
    assert api.delete(f"/api/ota/devices/{OLD_UDID}").json() == {"removed": True}
    assert api.delete("/api/ota/devices/nope").status_code == 400
    assert len(api.get("/api/ota/devices").json()["devices"]) == 1


def test_invite_code_gates_profile_download(api, monkeypatch):
    monkeypatch.setenv("KARRY_OTA_INVITE_CODE", "team-2026")
    assert api.get("/ota/profile").status_code == 403
    assert api.get("/ota/profile", params={"code": "wrong"}).status_code == 403
    assert api.get("/ota/profile", params={"code": "team-2026"}).status_code == 200
    assert 'name="code"' in api.get("/ota/", headers={"User-Agent": IOS_SAFARI}).text


def test_registry_survives_reload_and_memory_mode(tmp_path):
    path = tmp_path / "r.json"
    registry = ota.DeviceRegistry(str(path))
    token = registry.issue()
    registry.register(token, {"UDID": UDID, "PRODUCT": "iPhone16,2", "VERSION": "23A340"})
    assert ota.DeviceRegistry(str(path)).devices()[UDID]["version"] == "23A340"
    memory = ota.DeviceRegistry("memory")
    memory.register(memory.issue(), {"UDID": OLD_UDID})
    assert list(memory.devices()) == [OLD_UDID.upper()]
    assert not (tmp_path / "memory").exists()


def test_app_wiring_is_disabled_without_flag(monkeypatch):
    monkeypatch.delenv("KARRY_OTA_ENROLLMENT", raising=False)
    monkeypatch.setenv("KARRY_API_TOKEN", "owner-token-for-test")
    import app as web

    client = TestClient(web.app)
    assert client.get("/ota/").status_code == 404
    assert client.get("/api/ota/devices", headers={"Authorization": "Bearer owner-token-for-test"}).status_code == 404


def test_enrollment_dispatches_signing_workflow(api, monkeypatch):
    calls, done = [], threading.Event()

    def fake(udid, covered):
        calls.append((udid, covered)); done.set(); return "dispatched: HTTP 204"

    monkeypatch.setattr(ota, "dispatch_signing", fake)
    token, _ = issue(api)
    api.post(f"/api/register-udid/{token}", content=attributes(), follow_redirects=False)
    assert done.wait(5)
    assert calls == [(UDID, False)]


def test_dispatch_signing_requires_token_and_posts_workflow_inputs(monkeypatch):
    monkeypatch.delenv("KARRY_GITHUB_TOKEN", raising=False)
    assert ota.dispatch_signing(UDID, False).startswith("skipped: KARRY_GITHUB_TOKEN")
    monkeypatch.setenv("KARRY_GITHUB_TOKEN", "ghp_test")
    assert ota.dispatch_signing(UDID, True).startswith("skipped: current build")
    monkeypatch.setenv("KARRY_GITHUB_REPO", "bad repo name")
    assert ota.dispatch_signing(UDID, False).startswith("skipped: invalid")
    monkeypatch.setenv("KARRY_GITHUB_REPO", "owner/repo")
    seen = {}

    class Resp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(request, timeout):
        seen["url"] = request.full_url; seen["body"] = json.loads(request.data)
        seen["auth"] = request.get_header("Authorization"); return Resp()

    monkeypatch.setattr(ota.urllib.request, "urlopen", urlopen)
    assert ota.dispatch_signing(UDID, False) == "dispatched: HTTP 204"
    assert seen["url"] == "https://api.github.com/repos/owner/repo/actions/workflows/ota-karry-agent-2.yml/dispatches"
    assert seen["body"] == {"ref": "main", "inputs": {"udids": UDID}}
    assert seen["auth"] == "Bearer ghp_test"


def test_android_page_links_apk_and_pairing_ids(api, monkeypatch):
    page = api.get("/ota/android").text
    assert "releases/latest/download/karry-agent-1.apk" in page
    assert "observer-android" in page and "operation-iphone" in page
    monkeypatch.setenv("KARRY_ANDROID_APK_URL", "http://insecure.example/app.apk")
    assert "KARRY_ANDROID_APK_URL" in api.get("/ota/android").text
    assert '/ota/android' in api.get("/ota/", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 15) Chrome/130 Mobile"}).text
