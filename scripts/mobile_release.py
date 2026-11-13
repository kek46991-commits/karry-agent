#!/usr/bin/env python3
"""Build existing mobile companions without changing the developer's Xcode settings."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
WELL_KNOWN = '/.well-known/karry-agent-verification.txt'
FLAVORS = {'agent1': 'ai.karry.observer', 'agent3': 'ai.karry.observer.agent3'}
FIXTURE = 'karry-agent=LOCAL_TEST_ONLY_NOT_FOR_DOMAIN_APPROVAL'


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def public_hostname(value: str) -> str:
    host = value.encode('idna').decode('ascii').lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('A public domain name, not an IP address, is required')
    labels = host.split('.')
    if len(labels) < 2 or len(host) > 253 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels
    ) or host.endswith(('.localhost', '.local', '.invalid', '.test', '.example')):
        raise ValueError('Invalid public domain name')
    return host


def https_base(value: str) -> str:
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError('OTA URL cannot contain whitespace or control characters')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('OTA base URL must be public HTTPS without credentials/query/fragment')
    public_hostname(parsed.hostname)
    if parsed.port not in (None, 443):
        raise ValueError('OTA base URL must use HTTPS port 443')
    return value.rstrip('/')


@contextmanager
def loopback_server(content: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != WELL_KNOWN:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def prepare_verification(output: Path, domain: str | None, challenge_file: Path | None) -> dict:
    if bool(domain) != bool(challenge_file):
        raise ValueError('--domain and --challenge-file must be supplied together')
    host = public_hostname(domain) if domain else 'example.invalid'
    challenge = challenge_file.read_text().strip() if challenge_file else FIXTURE
    if not re.fullmatch(r'karry-agent=[A-Za-z0-9_-]{20,128}', challenge):
        raise ValueError('Expected the public karry-agent=... challenge issued by the authorization service, not an API token')
    directory = output / 'verification'
    well_known = directory / '.well-known'
    well_known.mkdir(parents=True)
    content = (challenge + '\n').encode()
    (well_known / 'karry-agent-verification.txt').write_bytes(content)
    (directory / 'dns.txt').write_text(f'_karry-agent-verification.{host}. IN TXT "{challenge}"\n')
    with loopback_server(content) as base, urlopen(base + WELL_KNOWN, timeout=5) as response:
        if response.read() != content:
            raise ValueError('Loopback fixture response mismatch')
    result = {
        'domain': host,
        'status': 'prepared_not_published' if domain else 'fixture_only',
        'domain_verified': False,
        'loopback_test': 'passed',
        'note': 'Local fixture test only; no production domain approval, DNS write, or verification bypass.',
    }
    write_json(directory / 'verification.json', result)
    write_json(directory / 'loopback-test.json', {
        'bind': '127.0.0.1', 'port': 0, 'path': WELL_KNOWN,
        'fixture': '.well-known/karry-agent-verification.txt', 'production_verifier_unchanged': True,
    })
    return result


def run(command: list[str], *, cwd: Path, env: dict | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def android_sdk() -> Path:
    candidates = [os.getenv('ANDROID_HOME'), os.getenv('ANDROID_SDK_ROOT'),
                  str(Path.home() / 'android-sdk'), str(Path.home() / 'Library/Android/sdk')]
    for candidate in candidates:
        if candidate and (Path(candidate) / 'platform-tools/adb').is_file():
            return Path(candidate)
    raise ValueError('Android SDK is missing (set ANDROID_HOME or ANDROID_SDK_ROOT)')


def build_android(output: Path, flavors: list[str], install: bool) -> dict:
    sdk = android_sdk()
    tools = sorted((sdk / 'build-tools').glob('*'), key=lambda p: tuple(int(n) for n in re.findall(r'\d+', p.name)))
    if not tools:
        raise ValueError('Android SDK build-tools are missing')
    build_tools = tools[-1]
    tasks = [task for flavor in flavors for task in (
        f':app:test{flavor.title()}DebugUnitTest', f':app:assemble{flavor.title()}Debug')]
    env = {**os.environ, 'ANDROID_HOME': str(sdk), 'ANDROID_SDK_ROOT': str(sdk)}
    run(['./gradlew', '--no-daemon', *tasks], cwd=ROOT / 'android-observer', env=env)
    directory = output / 'android'
    directory.mkdir()
    artifacts = []
    for flavor in flavors:
        source = ROOT / f'android-observer/app/build/outputs/apk/{flavor}/debug/app-{flavor}-debug.apk'
        run([str(build_tools / 'apksigner'), 'verify', str(source)], cwd=ROOT)
        metadata = subprocess.check_output([str(build_tools / 'aapt'), 'dump', 'badging', str(source)], text=True)
        if f"package: name='{FLAVORS[flavor]}'" not in metadata:
            raise ValueError('Unexpected Android application ID')
        destination = directory / f'Karry-Agent-{flavor[-1]}-debug.apk'
        shutil.copy2(source, destination)
        artifacts.append(destination.relative_to(output).as_posix())
    result = {'status': 'succeeded', 'artifacts': artifacts, 'tests': 'passed',
              'signing': 'debug_only', 'emulator_installed': False,
              'note': 'Physical Android installation may require unknown-app installation permission.'}
    if install:
        try:
            adb = str(sdk / 'platform-tools/adb')
            devices = subprocess.check_output([adb, 'devices'], text=True).splitlines()[1:]
            emulators = [line.split()[0] for line in devices if line.startswith('emulator-') and line.endswith('\tdevice')]
            if len(emulators) != 1:
                raise ValueError('Automatic install requires exactly one running authorized Android emulator')
            for relative in artifacts:
                run([adb, '-s', emulators[0], 'install', '-r', str(output / relative)], cwd=ROOT)
            result['emulator_installed'] = True
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            result['status'] = 'failed'
            result['blockers'] = ['APK build succeeded, emulator installation failed: ' + str(exc)]
    return result


def build_ios(output: Path, args: argparse.Namespace) -> dict:
    if sys.platform != 'darwin':
        return {'status': 'blocked', 'artifacts': [], 'blockers': ['iOS builds require macOS with Xcode.']}
    command = [sys.executable, str(ROOT / 'scripts/build_ios.py'), '--output', str(output / 'ios'),
               '--mode', args.ios_mode, '--bundle-id', args.bundle_id, '--scheme', args.ios_scheme]
    if args.ios_test:
        command.append('--test')
    for option in ('team_id', 'profile', 'signing_identity', 'signing_mode', 'api_key_path', 'api_key_id', 'api_issuer_id'):
        value = getattr(args, option)
        if value:
            command.extend(['--' + option.replace('_', '-'), str(value)])
    completed = subprocess.run(command, cwd=ROOT, check=False)
    result_path = output / 'ios/result.json'
    if not result_path.is_file():
        return {'status': 'failed', 'artifacts': [], 'blockers': ['iOS helper did not produce result.json.']}
    result = json.loads(result_path.read_text())
    result['artifacts'] = ['ios/' + artifact for artifact in result.get('artifacts', [])]
    if completed.returncode and result.get('status') == 'succeeded':
        result['status'] = 'failed'
    return result


def artifact_path(output: Path, relative: str) -> Path:
    path = (output / relative).resolve()
    if not path.is_relative_to(output.resolve()) or not path.exists():
        raise ValueError('Artifact must exist inside the output directory')
    return path


def prepare_ota(output: Path, ios: dict, base: str, bundle_id: str) -> dict:
    base = https_base(base)
    if ios.get('status') != 'succeeded' or ios.get('mode') != 'adhoc':
        return {'status': 'blocked', 'reason': 'OTA requires a successfully signed ad hoc IPA; unsigned/simulator builds are not installable.'}
    ipas = [artifact_path(output, name) for name in ios.get('artifacts', []) if name.endswith('.ipa')]
    if len(ipas) != 1:
        raise ValueError('Expected exactly one signed IPA')
    with zipfile.ZipFile(ipas[0]) as archive:
        infos = [name for name in archive.namelist() if re.fullmatch(r'Payload/[^/]+\.app/Info.plist', name)]
        if len(infos) != 1:
            raise ValueError('IPA must contain one top-level app')
        info = plistlib.loads(archive.read(infos[0]))
        if info['CFBundleIdentifier'] != bundle_id:
            raise ValueError('IPA bundle ID does not match requested OTA bundle ID')
        app = infos[0].removesuffix('Info.plist')
        if app + 'embedded.mobileprovision' not in archive.namelist() or app + '_CodeSignature/CodeResources' not in archive.namelist():
            raise ValueError('IPA lacks provisioning/signature metadata')
    directory = output / 'ota'
    directory.mkdir()
    shutil.copy2(ipas[0], directory / ipas[0].name)
    manifest = {'items': [{'assets': [{'kind': 'software-package', 'url': base + '/' + quote(ipas[0].name)}],
        'metadata': {'bundle-identifier': bundle_id, 'bundle-version': str(info['CFBundleVersion']),
                     'kind': 'software', 'title': str(info.get('CFBundleDisplayName', 'Karry-Agent'))}}]}
    (directory / 'manifest.plist').write_bytes(plistlib.dumps(manifest))
    from html import escape
    link = 'itms-services://?action=download-manifest&url=' + quote(base + '/manifest.plist', safe='')
    (directory / 'install.html').write_text('<!doctype html><meta charset="utf-8"><title>Karry-Agent install</title>'
        '<h1>Registered test devices only</h1><p>Requires valid HTTPS hosting and an eligible registered device.</p>'
        f'<a href="{escape(link, quote=True)}">Install signed test build</a>')
    return {'status': 'prepared_not_published', 'artifacts': ['ota'],
            'reason': 'Upload OTA files to the configured HTTPS host; no hosting/DNS changes were made.'}


def package(output: Path, result: dict) -> Path:
    paths = [output / 'verification']
    for name in ('android', 'ios', 'ota'):
        for relative in result.get(name, {}).get('artifacts', []):
            paths.append(artifact_path(output, relative))
    files = {p for path in paths for p in (path.rglob('*') if path.is_dir() else [path]) if p.is_file()}
    for path in files:
        if path.suffix.lower() in {'.p8', '.p12', '.key', '.pem'}:
            raise ValueError('Signing credentials cannot be packaged')
        if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
            raise ValueError('External/symlink artifacts cannot be packaged')
    sums = '# SHA256 artifact integrity only; NOT Apple signing or domain authorization.\n'
    for path in sorted(files):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        sums += f'{digest.hexdigest()}  {path.relative_to(output).as_posix()}\n'
    (output / 'app-verification.txt').write_text(sums)
    write_json(output / 'result.json', result)
    (output / 'START-HERE.txt').write_text(
        'Karry-Agent mobile test builds\n'
        'Android 1/3 are observers; iOS 2/4 are operators.\n'
        'Simulator apps run only on the iOS Simulator. Unsigned device .app files cannot be installed on an iPhone.\n'
        'Ad hoc installation requires Apple signing, a registered device, and OS installation approval.\n'
        'verification/ contains a local fixture or the supplied public challenge, NOT completed domain approval.\n'
        'Copy the full iPhone device ID into its Android observer; server URL/owner enrollment are still required.\n'
        'Backend deployment, production DNS and HTTPS hosting are NOT performed by this tool.\n'
        'Read result.json for succeeded, failed and blocked steps. No claim of zero human work is made.\n')
    files.update(output / name for name in ('app-verification.txt', 'result.json', 'START-HERE.txt'))
    target = output / 'Karry-mobile-builds.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(output))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / ('mobile-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
    parser.add_argument('--platform', choices=('all', 'android', 'ios', 'prepare'), default='all')
    parser.add_argument('--android-flavors', nargs='+', choices=tuple(FLAVORS), default=list(FLAVORS))
    parser.add_argument('--install-android', action='store_true')
    parser.add_argument('--ios-mode', choices=('simulator', 'unsigned', 'adhoc'), default='simulator')
    parser.add_argument('--ios-scheme', choices=('KarryOperator', 'KarryOperator4'), default='KarryOperator')
    parser.add_argument('--bundle-id', help='Default: com.kishi.karryagent2 (or karryagent4 for scheme KarryOperator4)')
    parser.add_argument('--ios-test', action='store_true')
    parser.add_argument('--team-id')
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--signing-identity')
    parser.add_argument('--signing-mode', choices=('automatic', 'manual'), default='automatic')
    parser.add_argument('--api-key-path', type=Path)
    parser.add_argument('--api-key-id')
    parser.add_argument('--api-issuer-id')
    parser.add_argument('--domain')
    parser.add_argument('--challenge-file', type=Path)
    parser.add_argument('--ota-base-url')
    args = parser.parse_args(argv)
    if args.bundle_id is None:
        args.bundle_id = 'com.kishi.karryagent4' if args.ios_scheme == 'KarryOperator4' else 'com.kishi.karryagent2'
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Output directory must be new or empty; stale artifacts will not be reused')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+', args.bundle_id):
        parser.error('Invalid bundle ID')
    if args.ota_base_url:
        try:
            https_base(args.ota_base_url)
        except ValueError as exc:
            parser.error(str(exc))
    output.mkdir(parents=True, exist_ok=True)
    result = {'status': 'succeeded', 'manual_work_remaining': True, 'scope': args.platform}
    try:
        result['verification'] = prepare_verification(output, args.domain, args.challenge_file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    for name, build in (('android', lambda: build_android(output, list(dict.fromkeys(args.android_flavors)), args.install_android)),
                        ('ios', lambda: build_ios(output, args))):
        if args.platform not in (name, 'all'):
            continue
        try:
            result[name] = build()
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            result[name] = {'status': 'failed', 'artifacts': [], 'blockers': [type(exc).__name__ + ': ' + str(exc)]}
    if args.ota_base_url:
        try:
            result['ota'] = prepare_ota(output, result.get('ios', {}), args.ota_base_url, args.bundle_id)
        except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
            result['ota'] = {'status': 'failed', 'artifacts': [], 'reason': str(exc)}
    if any(value.get('status') in ('blocked', 'failed') for value in result.values() if isinstance(value, dict)):
        result['status'] = 'incomplete'
    target = package(output, result)
    print(json.dumps({'status': result['status'], 'output': str(output), 'package': str(target),
                      'manual_work_remaining': True}, ensure_ascii=False))
    return 0 if result['status'] == 'succeeded' else 2


if __name__ == '__main__':
    raise SystemExit(main())
