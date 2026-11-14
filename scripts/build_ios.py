#!/usr/bin/env python3
"""Build the Karry-Agent iPhone operator apps without opening Xcode.

Modes:
  simulator  Debug build for an auto-selected iPhone simulator.
  unsigned   Release build for generic iOS with CODE_SIGNING_ALLOWED=NO (not installable).
  adhoc      Archive and export a release-testing (ad hoc) IPA using an installed
             identity/profile or explicit API-key automatic signing. Never falls back to unsigned.

Writes ``result.json`` into the fresh ``--output`` directory and exits 0 only on success.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "ios" / "KarryOperator" / "KarryOperator.xcodeproj"
SCHEMES = {
    "KarryOperator": {"product": "KarryOperator", "bundle_setting": "KARRY_BUNDLE_ID",
                      "default_bundle_id": "com.kishi.karryagent2", "testable": True},
    "KarryOperator4": {"product": "KarryOperator4", "bundle_setting": "KARRY4_BUNDLE_ID",
                       "default_bundle_id": "com.kishi.karryagent4", "testable": False},
}
PROFILE_DIRS = (
    Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles",
    Path.home() / "Library/MobileDevice/Provisioning Profiles",
)
DISTRIBUTION_PREFIXES = ("Apple Distribution", "iPhone Distribution")
BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
IDENTITY_RE = re.compile(r'^\s*\d+\)\s+([0-9A-F]{40})\s+"(.+)"\s*$')
DESTINATION_RE = re.compile(r"\{\s*platform:iOS Simulator,.*?id:([0-9A-F-]{36}),\s*OS:([\d.]+),\s*name:([^}]+?)\s*\}")
TESTS_RE = re.compile(r"Executed (\d+) tests?, with (?:(\d+) tests? skipped and )?(\d+) failures?")
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3
AUTH_FLAGS = ("-authenticationKeyPath", "-authenticationKeyID", "-authenticationKeyIssuerID")


class Blocked(Exception):
    def __init__(self, blockers: list[str]):
        super().__init__("; ".join(blockers))
        self.blockers = blockers


class Failed(Exception):
    pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, help="absolute path of a new or empty output directory")
    parser.add_argument("--mode", required=True, choices=("simulator", "unsigned", "adhoc"))
    parser.add_argument("--scheme", default="KarryOperator", choices=sorted(SCHEMES))
    parser.add_argument("--bundle-id", help="default: com.kishi.karryagent2 (KarryOperator) / com.kishi.karryagent4")
    parser.add_argument("--test", action="store_true", help="also run the XCTest suite on a simulator")
    parser.add_argument("--team-id", default=os.getenv("APPLE_TEAM_ID"), help="adhoc: Apple Developer Team ID (default: from the profile)")
    parser.add_argument("--profile", help="adhoc: path to a local .mobileprovision (default: installed profiles)")
    parser.add_argument("--signing-identity", help="adhoc: identity common name or SHA-1 to use")
    parser.add_argument("--signing-mode", choices=("automatic", "manual"), default="automatic")
    parser.add_argument("--api-key-path", default=os.getenv("APP_STORE_CONNECT_API_KEY_PATH"))
    parser.add_argument("--api-key-id", default=os.getenv("APP_STORE_CONNECT_API_KEY_ID"))
    parser.add_argument("--api-issuer-id", default=os.getenv("APP_STORE_CONNECT_API_ISSUER_ID"))
    return parser.parse_args(argv)


def run(command: list[str], log: Path, env: dict[str, str] | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    confidential = [command[i + 1] for i, flag in enumerate(command[:-1]) if flag in AUTH_FLAGS]

    def redact(text: str) -> str:
        for value in confidential:
            text = text.replace(value, "[REDACTED]")
        return text

    with log.open("w", encoding="utf-8") as handle:
        handle.write("$ " + redact(" ".join(command)) + "\n\n")
        handle.flush()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", env=env) as process:
            for line in process.stdout:
                handle.write(redact(line))
            return process.wait()


def capture(command: list[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=False).stdout


def require_tools() -> None:
    if sys.platform != "darwin" or not shutil.which("xcodebuild"):
        raise Blocked(["xcodebuild was not found: a Mac with Xcode installed is required"])


def parse_destinations(text: str) -> list[dict[str, str]]:
    found = []
    for udid, version, name in DESTINATION_RE.findall(text):
        if name.startswith("iPhone"):
            found.append({"id": udid, "os": version, "name": name})
    return sorted(found, key=lambda d: (tuple(int(p) for p in d["os"].split(".")), d["name"]), reverse=True)


def select_simulator(scheme: str) -> dict[str, str]:
    # CoreSimulator can briefly report no destinations while another xcodebuild is starting.
    for attempt in range(3):
        text = capture(["xcodebuild", "-project", str(PROJECT), "-scheme", scheme, "-showdestinations"])
        destinations = parse_destinations(text)
        if destinations:
            break
        time.sleep(5 * (attempt + 1))
    else:
        raise Blocked(["no available iPhone simulator for this Xcode; install an iOS simulator runtime"])
    return destinations[0]


def parse_tests(text: str) -> dict[str, int] | None:
    matches = TESTS_RE.findall(text)
    if not matches:
        return None
    executed, skipped, failures = matches[-1]
    return {"executed": int(executed), "skipped": int(skipped or 0), "failures": int(failures)}


def bundle_id_of(app: Path) -> str:
    with (app / "Info.plist").open("rb") as handle:
        return plistlib.load(handle).get("CFBundleIdentifier", "")


def copy_app(source: Path, destination: Path, bundle_id: str) -> None:
    if not source.is_dir():
        raise Failed(f"build reported success but {source.name} was not produced")
    if bundle_id_of(source) != bundle_id:
        raise Failed(f"{source.name} has bundle ID {bundle_id_of(source)!r}, expected {bundle_id!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)


def list_identities() -> list[tuple[str, str]]:
    """Valid code-signing identities (certificate with private key) as (SHA-1, common name)."""
    if not shutil.which("security"):
        return []
    text = capture(["security", "find-identity", "-v", "-p", "codesigning"])
    return [match.groups() for match in map(IDENTITY_RE.match, text.splitlines()) if match]


def load_profile(path: Path) -> dict | None:
    result = subprocess.run(["security", "cms", "-D", "-i", str(path)], capture_output=True, check=False)
    if result.returncode != 0:
        return None
    try:
        return plistlib.loads(result.stdout)
    except plistlib.InvalidFileException:
        return None


def find_profiles(explicit: str | None) -> list[tuple[Path, dict]]:
    paths = [Path(explicit)] if explicit else sorted(
        p for directory in PROFILE_DIRS if directory.is_dir() for p in directory.glob("*.mobileprovision"))
    profiles = []
    for path in paths:
        profile = load_profile(path) if path.is_file() else None
        if profile is not None:
            profiles.append((path, profile))
    return profiles


def profile_problems(profile: dict, bundle_id: str, team_id: str | None,
                     identities: list[tuple[str, str]], now: dt.datetime, *, check_identity: bool = True) -> tuple[list[str], str | None]:
    """Why ``profile`` cannot sign an ad hoc build of ``bundle_id``, and the matching identity SHA-1."""
    problems = []
    expiry = profile.get("ExpirationDate")
    if not isinstance(expiry, dt.datetime) or expiry <= now:
        problems.append("expired")
    teams = profile.get("TeamIdentifier") or []
    if team_id and team_id not in teams:
        problems.append("different team")
    entitlements = profile.get("Entitlements") or {}
    app_id = str(entitlements.get("application-identifier", ""))
    _, _, pattern = app_id.partition(".")
    if not pattern or not (pattern == bundle_id or (pattern.endswith("*") and bundle_id.startswith(pattern[:-1]))):
        problems.append(f"App ID does not cover {bundle_id}")
    if entitlements.get("get-task-allow"):
        problems.append("development profile, not ad hoc")
    if profile.get("ProvisionsAllDevices"):
        problems.append("enterprise profile, not ad hoc")
    elif not profile.get("ProvisionedDevices"):
        problems.append("no registered devices")
    certificates = {hashlib.sha1(cert).hexdigest().upper() for cert in profile.get("DeveloperCertificates") or []}
    matching = [sha for sha, name in identities if sha in certificates and name.startswith(DISTRIBUTION_PREFIXES)]
    if check_identity and not matching:
        problems.append("no installed distribution certificate with private key matches")
    return problems, (matching[0] if matching else None)


def choose_signing(args: argparse.Namespace, bundle_id: str) -> tuple[Path, dict, str, str]:
    if args.team_id and not TEAM_ID_RE.match(args.team_id):
        raise Blocked(["--team-id must be a 10-character Apple Team ID"])
    identities = list_identities()
    if args.signing_identity:
        identities = [(sha, name) for sha, name in identities if args.signing_identity in (sha, name)]
    blockers = []
    if not identities:
        blockers.append("no valid code-signing identity (certificate + private key) is installed in the keychain"
                        + (" matching --signing-identity" if args.signing_identity else ""))
    elif not any(name.startswith(DISTRIBUTION_PREFIXES) for _, name in identities):
        blockers.append("no Apple Distribution identity is installed (development identities cannot sign ad hoc)")
    profiles = find_profiles(args.profile)
    if not profiles:
        blockers.append("no readable provisioning profile found" + (
            f" at {args.profile}" if args.profile else " in ~/Library/Developer/Xcode/UserData/Provisioning Profiles"))
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for path, profile in profiles:
        problems, identity = profile_problems(profile, bundle_id, args.team_id, identities, now)
        if not problems and identity:
            team = args.team_id or (profile.get("TeamIdentifier") or [""])[0]
            return path, profile, identity, team
        blockers.append(f"profile {profile.get('Name', path.name)!r}: " + ", ".join(problems))
    raise Blocked(blockers)


def ensure_profile_installed(path: Path, uuid: str) -> None:
    if any((directory / f"{uuid}.mobileprovision").is_file() for directory in PROFILE_DIRS):
        return
    PROFILE_DIRS[0].mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, PROFILE_DIRS[0] / f"{uuid}.mobileprovision")


def automatic_signing(args: argparse.Namespace) -> tuple[list[str], dict]:
    blockers = []
    if args.profile or args.signing_identity:
        blockers.append("manual profile/identity options require --signing-mode manual")
    if not args.team_id or not TEAM_ID_RE.fullmatch(args.team_id):
        blockers.append("APPLE_TEAM_ID / --team-id must identify the authorized Apple Developer team")
    if not args.api_key_id or not re.fullmatch(r"[A-Z0-9]{10}", args.api_key_id):
        blockers.append("APP_STORE_CONNECT_API_KEY_ID / --api-key-id is required (10 uppercase letters/digits)")
    if not args.api_issuer_id or not re.fullmatch(r"[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}", args.api_issuer_id):
        blockers.append("APP_STORE_CONNECT_API_ISSUER_ID / --api-issuer-id is required (team-key issuer UUID)")
    path = Path(args.api_key_path).expanduser().resolve() if args.api_key_path else None
    if path is None or not path.is_file() or not os.access(path, os.R_OK) or path.stat().st_size == 0:
        blockers.append("APP_STORE_CONNECT_API_KEY_PATH / --api-key-path must point to a readable team API private key")
    if blockers:
        raise Blocked(blockers + ["No fallback to the Mac iCloud/Xcode account; authorize an Apple Developer team first."])
    authentication = ["-allowProvisioningUpdates", "-authenticationKeyPath", str(path),
                      "-authenticationKeyID", args.api_key_id, "-authenticationKeyIssuerID", args.api_issuer_id]
    options = {"method": "release-testing", "signingStyle": "automatic", "teamID": args.team_id, "thinning": "<none>"}
    return authentication, options


def verify_ipa(ipa: Path, bundle_id: str, work: Path, team_id: str) -> dict:
    extracted = work / "ipa-check"
    with zipfile.ZipFile(ipa) as archive:
        archive.extractall(extracted)
    apps = list((extracted / "Payload").glob("*.app"))
    if len(apps) != 1 or bundle_id_of(apps[0]) != bundle_id:
        raise Failed("exported IPA does not contain exactly one app with the requested bundle ID")
    if not (apps[0] / "embedded.mobileprovision").is_file():
        raise Failed("exported IPA has no embedded provisioning profile")
    profile = load_profile(apps[0] / "embedded.mobileprovision")
    if profile is None:
        raise Failed("exported IPA has an unreadable embedded provisioning profile")
    problems, _ = profile_problems(profile, bundle_id, team_id, [],
                                   dt.datetime.now(dt.timezone.utc).replace(tzinfo=None), check_identity=False)
    if problems:
        raise Failed("exported IPA is not valid ad hoc provisioning: " + "; ".join(problems))
    if subprocess.run(["codesign", "--verify", "--strict", str(apps[0])], capture_output=True, check=False).returncode != 0:
        raise Failed("codesign verification of the exported app failed")
    prefix = work / "signer-"
    extracted = subprocess.run(["codesign", "--display", f"--extract-certificates={prefix}", str(apps[0])],
                               capture_output=True, check=False)
    leaf = work / "signer-0"
    if extracted.returncode or not leaf.is_file() or leaf.read_bytes() not in profile.get("DeveloperCertificates", []):
        raise Failed("exported IPA signer does not match the embedded profile")
    return {"team_id": team_id, "registered_devices": len(profile["ProvisionedDevices"])}


def build(args: argparse.Namespace, bundle_id: str, out: Path, work: Path, result: dict) -> None:
    scheme = SCHEMES[args.scheme]
    product, setting = scheme["product"], f"{scheme['bundle_setting']}={bundle_id}"
    base = ["xcodebuild", "-project", str(PROJECT), "-scheme", args.scheme, "-derivedDataPath", str(work / "dd")]
    logs = out / "logs"
    automatic = None
    if args.mode == "adhoc" and args.signing_mode == "automatic":
        automatic = automatic_signing(args)
    result["xcode"] = capture(["xcodebuild", "-version"]).splitlines()[:1]

    simulator = None
    if args.mode == "simulator" or args.test:
        simulator = select_simulator(args.scheme)
        result["simulator"] = simulator

    if args.mode == "simulator":
        if run(base + ["-configuration", "Debug", "-destination", f"id={simulator['id']}", setting, "build"],
               logs / "simulator-build.log") != 0:
            raise Failed("simulator build failed (see logs/simulator-build.log)")
        copy_app(work / f"dd/Build/Products/Debug-iphonesimulator/{product}.app",
                 out / f"simulator/{product}.app", bundle_id)
        result["artifacts"].append(f"simulator/{product}.app")
    elif args.mode == "unsigned":
        if run(base + ["-configuration", "Release", "-destination", "generic/platform=iOS",
                       "CODE_SIGNING_ALLOWED=NO", setting, "build"], logs / "unsigned-build.log") != 0:
            raise Failed("unsigned device build failed (see logs/unsigned-build.log)")
        folder = "unsigned-device-NOT-INSTALLABLE"
        copy_app(work / f"dd/Build/Products/Release-iphoneos/{product}.app", out / f"{folder}/{product}.app", bundle_id)
        (out / folder / "README.txt").write_text(
            "This .app is NOT signed and cannot be installed on an iPhone. It is not an IPA.\n"
            "Run build_ios.py --mode adhoc with authorized Apple team API credentials,\n"
            "or --signing-mode manual with an installed distribution identity and ad hoc profile.\n", encoding="utf-8")
        result["artifacts"].append(f"{folder}/{product}.app")
    else:
        authentication = []
        if automatic:
            authentication, export_options = automatic
            team = args.team_id
            signing = [f"DEVELOPMENT_TEAM={team}", "CODE_SIGN_STYLE=Automatic", "CODE_SIGN_IDENTITY=Apple Development",
                       "PROVISIONING_PROFILE_SPECIFIER=", "PROVISIONING_PROFILE="]
        else:
            path, profile, identity, team = choose_signing(args, bundle_id)
            uuid = profile["UUID"]
            ensure_profile_installed(path, uuid)
            signing = [f"DEVELOPMENT_TEAM={team}", "CODE_SIGN_STYLE=Manual", f"CODE_SIGN_IDENTITY={identity}",
                       f"PROVISIONING_PROFILE_SPECIFIER={uuid}"]
            export_options = {"method": "release-testing", "signingStyle": "manual", "teamID": team,
                              "signingCertificate": identity, "provisioningProfiles": {bundle_id: uuid}, "thinning": "<none>"}
        archive = work / f"{product}.xcarchive"
        if run(base + ["-configuration", "Release", "-destination", "generic/platform=iOS",
                       "-archivePath", str(archive), setting, *signing, *authentication, "archive"], logs / "adhoc-archive.log") != 0:
            raise Failed("ad hoc archive/provisioning failed (see logs/adhoc-archive.log; check Apple permissions and registered devices)")
        options = work / "ExportOptions.plist"
        options.write_bytes(plistlib.dumps(export_options))
        if run(["xcodebuild", "-exportArchive", "-archivePath", str(archive), "-exportPath", str(out / "adhoc"),
                "-exportOptionsPlist", str(options), *authentication], logs / "adhoc-export.log") != 0:
            raise Failed("ad hoc export failed (see logs/adhoc-export.log)")
        ipas = sorted((out / "adhoc").glob("*.ipa"))
        if len(ipas) != 1:
            raise Failed("ad hoc export did not produce exactly one IPA")
        verified = verify_ipa(ipas[0], bundle_id, work, team)
        result["artifacts"].append(str(ipas[0].relative_to(out)))
        result["signing"] = {"mode": args.signing_mode, **verified}

    if args.test:
        run_tests(args, base, setting, bundle_id, simulator, logs, result)


def run_tests(args, base, setting, bundle_id, simulator, logs, result) -> None:
    if not SCHEMES[args.scheme]["testable"]:
        result["tests"] = {"status": "blocked"}
        raise Blocked([f"scheme {args.scheme} has no XCTest target; run --test with --scheme KarryOperator"])
    env = {**os.environ, "TEST_RUNNER_KARRY_EXPECTED_BUNDLE_ID": bundle_id}
    code = run(base + ["-destination", f"id={simulator['id']}", setting, "test"], logs / "tests.log", env=env)
    counts = parse_tests((logs / "tests.log").read_text(encoding="utf-8", errors="replace")) or {}
    passed = code == 0 and counts.get("executed", 0) > counts.get("skipped", 0) and counts.get("failures") == 0
    result["tests"] = {"status": "passed" if passed else "failed", **counts}
    if result["tests"]["status"] != "passed":
        raise Failed("XCTest failed (see logs/tests.log)")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.output)
    bundle_id = args.bundle_id or SCHEMES[args.scheme]["default_bundle_id"]
    if not out.is_absolute():
        print("--output must be an absolute path", file=sys.stderr)
        return EXIT_USAGE
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"--output {out} is not empty; use a fresh directory", file=sys.stderr)
        return EXIT_USAGE
    if not BUNDLE_ID_RE.match(bundle_id):
        print(f"invalid --bundle-id {bundle_id!r}", file=sys.stderr)
        return EXIT_USAGE
    out.mkdir(parents=True, exist_ok=True)
    result = {"status": "failed", "mode": args.mode, "scheme": args.scheme, "bundle_id": bundle_id,
              "artifacts": [], "blockers": [], "tests": {"status": "requested" if args.test else "not_requested"}}
    if args.mode == "adhoc":
        result["signing_mode"] = args.signing_mode
    work = Path(tempfile.mkdtemp(prefix="karry-ios-"))
    try:
        require_tools()
        build(args, bundle_id, out, work, result)
        result["status"] = "succeeded"
    except Blocked as exc:
        result["status"], result["blockers"] = "blocked", exc.blockers
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        result["status"], result["blockers"] = "failed", [f"local build input/output failed ({type(exc).__name__})"]
    except Failed as exc:
        result["status"], result["blockers"] = "failed", [str(exc)]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if any((out / "logs").glob("*.log")):
        result["logs"] = "logs/"
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"succeeded": EXIT_OK, "blocked": EXIT_BLOCKED}.get(result["status"], EXIT_FAILED)


if __name__ == "__main__":
    sys.exit(main())
