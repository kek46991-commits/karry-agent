import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

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
    assert build_ios.main(["--output", str(out), "--mode", "adhoc", "--scheme", "KarryOperator4"]) == 3
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
