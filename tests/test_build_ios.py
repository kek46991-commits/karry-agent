import datetime as dt
import hashlib
import importlib.util
import json
import plistlib
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("build_ios", Path(__file__).resolve().parents[1] / "scripts" / "build_ios.py")
build_ios = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_ios)

NOW = dt.datetime(2026, 11, 13)
CERT = b"fake-distribution-certificate"
SHA = hashlib.sha1(CERT).hexdigest().upper()
DIST = [(SHA, "Apple Distribution: Kishi (ABCDE12345)")]


def profile(**overrides):
    base = {
        "Name": "Karry ad hoc",
        "UUID": "11111111-2222-3333-4444-555555555555",
        "ExpirationDate": NOW + dt.timedelta(days=30),
        "TeamIdentifier": ["ABCDE12345"],
        "Entitlements": {"application-identifier": "ABCDE12345.com.kishi.karryagent2", "get-task-allow": False},
        "ProvisionedDevices": ["00008110-000000000000001E"],
        "DeveloperCertificates": [CERT],
    }
    base.update(overrides)
    return base


def test_matching_ad_hoc_profile_has_no_problems():
    assert build_ios.profile_problems(profile(), "com.kishi.karryagent2", "ABCDE12345", DIST, NOW) == ([], SHA)


@pytest.mark.parametrize(
    ("overrides", "identities", "team", "expected"),
    [
        ({"ExpirationDate": NOW - dt.timedelta(days=1)}, DIST, None, "expired"),
        ({}, DIST, "ZZZZZ99999", "different team"),
        ({"Entitlements": {"application-identifier": "ABCDE12345.com.other.app"}}, DIST, None, "App ID does not cover"),
        ({"Entitlements": {"application-identifier": "ABCDE12345.com.kishi.karryagent2", "get-task-allow": True}},
         DIST, None, "development profile"),
        ({"ProvisionedDevices": []}, DIST, None, "no registered devices"),
        ({"ProvisionsAllDevices": True}, DIST, None, "enterprise profile"),
        ({}, [], None, "no installed distribution certificate"),
        ({}, [(SHA, "Apple Development: Kishi (ABCDE12345)")], None, "no installed distribution certificate"),
    ],
)
def test_profile_problems_reject_unusable_signing(overrides, identities, team, expected):
    problems, identity = build_ios.profile_problems(profile(**overrides), "com.kishi.karryagent2", team, identities, NOW)
    assert any(expected in problem for problem in problems)
    if not identities or "Development" in identities[0][1]:
        assert identity is None


def test_wildcard_app_id_covers_bundle_id():
    wildcard = profile(Entitlements={"application-identifier": "ABCDE12345.com.kishi.*"})
    assert build_ios.profile_problems(wildcard, "com.kishi.karryagent2", None, DIST, NOW)[0] == []


def test_destinations_prefer_newest_iphone_simulator():
    text = """
        { platform:iOS Simulator, id:dvtdevice-DVTiOSDeviceSimulatorPlaceholder-iphonesimulator:placeholder, name:Any iOS Simulator Device }
        { platform:iOS Simulator, arch:arm64, id:AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA, OS:26.5, name:iPhone 17 }
        { platform:iOS Simulator, arch:arm64, id:BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB, OS:26.10, name:iPhone 17 Pro }
        { platform:iOS Simulator, arch:arm64, id:CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC, OS:27.0, name:iPad Pro }
    """
    assert build_ios.parse_destinations(text)[0] == {
        "id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "os": "26.10", "name": "iPhone 17 Pro"}


def test_parse_tests_reads_last_summary():
    log = ("Executed 9 tests, with 0 failures (0 unexpected)\n"
           "Executed 42 tests, with 1 test skipped and 0 failures (0 unexpected)\n")
    assert build_ios.parse_tests(log) == {"executed": 42, "skipped": 1, "failures": 0}
    assert build_ios.parse_tests("** BUILD FAILED **") is None


def test_rejects_stale_output_without_touching_it(tmp_path):
    (tmp_path / "old.ipa").write_text("stale")
    assert build_ios.main(["--output", str(tmp_path), "--mode", "simulator"]) == build_ios.EXIT_USAGE
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.ipa"]


def test_rejects_relative_output_and_bad_bundle_id(tmp_path):
    assert build_ios.main(["--output", "relative", "--mode", "unsigned"]) == build_ios.EXIT_USAGE
    assert build_ios.main(["--output", str(tmp_path / "o"), "--mode", "unsigned", "--bundle-id", "bad id"]) == 2


def test_blocked_without_xcode(tmp_path, monkeypatch):
    monkeypatch.setattr(build_ios.sys, "platform", "linux")
    out = tmp_path / "out"
    assert build_ios.main(["--output", str(out), "--mode", "adhoc"]) == build_ios.EXIT_BLOCKED
    result = json.loads((out / "result.json").read_text())
    assert result["status"] == "blocked" and result["artifacts"] == []
    assert result["bundle_id"] == "com.kishi.karryagent2"


def test_adhoc_without_credentials_is_blocked_and_never_unsigned(tmp_path, monkeypatch):
    monkeypatch.setattr(build_ios, "require_tools", lambda: None)
    monkeypatch.setattr(build_ios, "capture", lambda command: "Xcode 26.6\n")
    monkeypatch.setattr(build_ios, "list_identities", list)
    monkeypatch.setattr(build_ios, "find_profiles", lambda explicit: [])
    monkeypatch.setattr(build_ios, "run", lambda *a, **k: pytest.fail("must not build without signing"))
    out = tmp_path / "out"
    assert build_ios.main(["--output", str(out), "--mode", "adhoc", "--scheme", "KarryOperator4", "--signing-mode", "manual"]) == 3
    result = json.loads((out / "result.json").read_text())
    assert result["status"] == "blocked"
    assert result["bundle_id"] == "com.kishi.karryagent4"
    assert result["artifacts"] == []
    assert any("code-signing identity" in b for b in result["blockers"])
    assert any("provisioning profile" in b for b in result["blockers"])
    assert sorted(p.name for p in out.iterdir()) == ["result.json"]


def test_io_failure_still_writes_result(tmp_path, monkeypatch):
    monkeypatch.setattr(build_ios, "require_tools", lambda: None)

    def fail(*args):
        raise OSError("unavailable build output")

    monkeypatch.setattr(build_ios, "build", fail)
    assert build_ios.main(["--output", str(tmp_path), "--mode", "unsigned"]) == build_ios.EXIT_FAILED
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "failed"
    assert result["artifacts"] == []


@pytest.mark.parametrize("summary", [
    "Executed 0 tests, with 0 failures", "Executed 2 tests, with 2 tests skipped and 0 failures",
    "Executed 2 tests, with 1 failures",
])
def test_empty_skipped_or_failed_suite_is_not_a_pass(tmp_path, monkeypatch, summary):
    args = build_ios.parse_args(["--output", str(tmp_path), "--mode", "simulator", "--test"])

    def fake_run(command, log, **kwargs):
        log.write_text(summary)
        return 0

    monkeypatch.setattr(build_ios, "run", fake_run)
    with pytest.raises(build_ios.Failed, match="XCTest failed"):
        build_ios.run_tests(args, [], "setting", "com.kishi.karryagent2", {"id": "test"}, tmp_path, {})


@pytest.fixture(autouse=True)
def clear_apple_environment(monkeypatch):
    for name in ("APPLE_TEAM_ID", "APP_STORE_CONNECT_API_KEY_PATH", "APP_STORE_CONNECT_API_KEY_ID",
                 "APP_STORE_CONNECT_API_ISSUER_ID"):
        monkeypatch.delenv(name, raising=False)


def automatic_args(tmp_path):
    key = tmp_path / "test-only.p8"
    key.write_text("not-a-real-key; mocked subprocesses only")
    return build_ios.parse_args(["--output", str(tmp_path / "output"), "--mode", "adhoc",
                                 "--team-id", "ABCDE12345", "--api-key-path", str(key),
                                 "--api-key-id", "TESTKEY123", "--api-issuer-id", "a" * 8 + "-bbbb-cccc-dddd-" + "e" * 12])


def test_automatic_missing_auth_does_not_start_xcode(tmp_path, monkeypatch):
    monkeypatch.setattr(build_ios, "require_tools", lambda: None)
    monkeypatch.setattr(build_ios, "capture", lambda *a: pytest.fail("must not start Xcode"))
    monkeypatch.setattr(build_ios, "run", lambda *a, **k: pytest.fail("must not build"))
    assert build_ios.main(["--output", str(tmp_path), "--mode", "adhoc"]) == 3
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["status"] == "blocked" and result["artifacts"] == []
    assert result["signing_mode"] == "automatic"
    assert any("No fallback" in reason for reason in result["blockers"])


@pytest.mark.parametrize(("field", "value"), [
    ("team_id", "invalid"), ("api_key_id", "invalid"), ("api_issuer_id", "invalid"),
    ("api_key_path", "/nonexistent/test-only.p8"), ("api_key_path", None),
    ("profile", "test.mobileprovision"), ("signing_identity", "test-identity"),
])
def test_automatic_rejects_bad_auth_and_manual_options(tmp_path, field, value):
    args = automatic_args(tmp_path)
    setattr(args, field, value)
    with pytest.raises(build_ios.Blocked):
        build_ios.automatic_signing(args)


def test_automatic_env_defaults_and_explicit_override(tmp_path, monkeypatch):
    args = automatic_args(tmp_path)
    for field, name in (("team_id", "APPLE_TEAM_ID"), ("api_key_path", "APP_STORE_CONNECT_API_KEY_PATH"),
                        ("api_key_id", "APP_STORE_CONNECT_API_KEY_ID"), ("api_issuer_id", "APP_STORE_CONNECT_API_ISSUER_ID")):
        monkeypatch.setenv(name, getattr(args, field))
    env_args = build_ios.parse_args(["--output", str(tmp_path), "--mode", "adhoc"])
    assert build_ios.automatic_signing(env_args) == build_ios.automatic_signing(args)
    override = build_ios.parse_args(["--output", str(tmp_path), "--mode", "adhoc", "--team-id", "OTHER12345"])
    assert override.team_id == "OTHER12345"


@pytest.mark.parametrize("failure_stage", [None, "archive", "export", "validation"])
def test_automatic_archive_export_flags_and_no_unsigned_fallback(tmp_path, monkeypatch, failure_stage):
    args = automatic_args(tmp_path)
    out, work = tmp_path / "output", tmp_path / "work"
    out.mkdir()
    work.mkdir()
    monkeypatch.setattr(build_ios, "capture", lambda *a: "Xcode 26.6")
    monkeypatch.setattr(build_ios, "choose_signing", lambda *a: pytest.fail("no manual signing lookup"))
    commands = []

    def fake_run(command, log):
        commands.append(command)
        stage = "export" if "-exportArchive" in command else "archive"
        if stage == "export":
            options = plistlib.loads(Path(command[command.index("-exportOptionsPlist") + 1]).read_bytes())
            assert options == {"method": "release-testing", "signingStyle": "automatic",
                               "teamID": args.team_id, "thinning": "<none>"}
            (out / "adhoc").mkdir()
            (out / "adhoc/app.ipa").write_bytes(b"mocked export")
        return 65 if stage == failure_stage else 0

    def verify(*a):
        if failure_stage == "validation":
            raise build_ios.Failed("bad IPA")
        return {"team_id": args.team_id, "registered_devices": 1}

    monkeypatch.setattr(build_ios, "run", fake_run)
    monkeypatch.setattr(build_ios, "verify_ipa", verify)
    result = {"artifacts": []}
    if failure_stage:
        with pytest.raises(build_ios.Failed):
            build_ios.build(args, "com.kishi.karryagent2", out, work, result)
        assert result["artifacts"] == []
    else:
        build_ios.build(args, "com.kishi.karryagent2", out, work, result)
        assert result["artifacts"] == ["adhoc/app.ipa"]
        assert result["signing"]["mode"] == "automatic"
    assert len(commands) == (1 if failure_stage == "archive" else 2)
    for command in commands:
        assert "-allowProvisioningUpdates" in command
        assert "-allowProvisioningDeviceRegistration" not in command
        assert "CODE_SIGNING_ALLOWED=NO" not in command
        for flag, value in zip(build_ios.AUTH_FLAGS, (args.api_key_path, args.api_key_id, args.api_issuer_id), strict=True):
            assert command[command.index(flag) + 1] == value
    assert "CODE_SIGN_STYLE=Automatic" in commands[0]


def test_auth_values_redacted_from_command_and_live_output(tmp_path):
    command = [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))"]
    values = ("/private/test-key.p8", "TESTKEY123", "issuer-test-value")
    for flag, value in zip(build_ios.AUTH_FLAGS, values, strict=True):
        command += [flag, value]
    log = tmp_path / "run.log"
    assert build_ios.run(command, log) == 0
    contents = log.read_text()
    assert all(value not in contents for value in values)
    assert contents.count("[REDACTED]") == 6


@pytest.mark.parametrize("problem", [None, "development", "expired", "team", "devices", "enterprise", "signer", "signature", "unreadable"])
def test_ipa_validation_checks_ad_hoc_profile_and_actual_signer(tmp_path, monkeypatch, problem):
    ipa = tmp_path / "app.ipa"
    with zipfile.ZipFile(ipa, "w") as archive:
        archive.writestr("Payload/App.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.kishi.karryagent2"}))
        archive.writestr("Payload/App.app/embedded.mobileprovision", b"mocked CMS")
    supplied = profile(ExpirationDate=dt.datetime.now() + dt.timedelta(days=1))
    if problem == "development":
        supplied["Entitlements"]["get-task-allow"] = True
    elif problem == "expired":
        supplied["ExpirationDate"] = dt.datetime.now() - dt.timedelta(days=1)
    elif problem == "team":
        supplied["TeamIdentifier"] = ["OTHER12345"]
    elif problem == "devices":
        supplied["ProvisionedDevices"] = []
    elif problem == "enterprise":
        supplied["ProvisionsAllDevices"] = True
    monkeypatch.setattr(build_ios, "load_profile", lambda *a: None if problem == "unreadable" else supplied)

    def fake_run(command, **kwargs):
        extract = [arg for arg in command if arg.startswith("--extract-certificates")]
        if extract:
            assert extract == [f"--extract-certificates={tmp_path / 'signer-'}"], "prefix must be attached with ="
            (tmp_path / "signer-0").write_bytes(b"wrong signer" if problem == "signer" else CERT)
        return SimpleNamespace(returncode=1 if problem == "signature" else 0)

    monkeypatch.setattr(build_ios.subprocess, "run", fake_run)
    if problem:
        with pytest.raises(build_ios.Failed):
            build_ios.verify_ipa(ipa, "com.kishi.karryagent2", tmp_path, "ABCDE12345")
    else:
        assert build_ios.verify_ipa(ipa, "com.kishi.karryagent2", tmp_path, "ABCDE12345")["registered_devices"] == 1
