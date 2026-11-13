import json
import plistlib
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from scripts import mobile_release as release


def test_fixture_verification_is_local_only(tmp_path):
    result = release.prepare_verification(tmp_path, None, None)
    assert result['status'] == 'fixture_only'
    assert result['domain_verified'] is False
    assert result['loopback_test'] == 'passed'
    assert (tmp_path / 'verification/.well-known/karry-agent-verification.txt').read_text().strip() == release.FIXTURE
    assert '_karry-agent-verification.example.invalid.' in (tmp_path / 'verification/dns.txt').read_text()


def test_issued_challenge_is_preserved_but_not_approved(tmp_path):
    challenge = tmp_path / 'challenge.txt'
    challenge.write_text('karry-agent=' + 'a' * 32)
    result = release.prepare_verification(tmp_path, 'Owner.Example.COM', challenge)
    assert result['status'] == 'prepared_not_published'
    assert result['domain_verified'] is False
    assert result['domain'] == 'owner.example.com'
    assert challenge.read_bytes() + b'\n' == (tmp_path / 'verification/.well-known/karry-agent-verification.txt').read_bytes()


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', 'abc.local', 'x.invalid', 'https://example.com',
                                 'example.com\nTXT injected', 'a..com', '-a.com', 'a-.com', 'example.com/path'])
def test_bad_domain_rejected(host):
    with pytest.raises(ValueError):
        release.public_hostname(host)


@pytest.mark.parametrize('url', ['http://example.com', 'https://u:p@example.com', 'https://127.0.0.1',
                                'https://example.com?q=1', 'https://example.com/#x', 'https://example.com:1234', 'https://exa\nmple.com'])
def test_invalid_ota_urls_rejected(url):
    with pytest.raises(ValueError):
        release.https_base(url)


def test_challenge_cannot_be_injected_or_used_without_domain(tmp_path):
    challenge = tmp_path / 'challenge'
    challenge.write_text('karry-agent=' + 'a' * 32 + '\nmalicious')
    with pytest.raises(ValueError):
        release.prepare_verification(tmp_path, 'example.com', challenge)
    with pytest.raises(ValueError):
        release.prepare_verification(tmp_path, None, challenge)
    with pytest.raises(ValueError):
        release.prepare_verification(tmp_path, 'example.com', None)


def test_loopback_has_no_directory_listing_or_auth_bypass():
    with release.loopback_server(b'local-only') as base:
        assert base.startswith('http://127.0.0.1:')
        with urlopen(base + release.WELL_KNOWN, timeout=5) as response:
            assert response.read() == b'local-only'
        for path in ['/', '/api/mobile-agent/devices/enroll', '/../../.env']:
            with pytest.raises(HTTPError) as error:
                urlopen(base + path, timeout=5)
            assert error.value.code == 404


def test_prepare_cli_packages_without_claiming_no_manual_work(tmp_path):
    output = tmp_path / 'build'
    assert release.main(['--platform', 'prepare', '--output', str(output)]) == 0
    result = json.loads((output / 'result.json').read_text())
    assert result['manual_work_remaining'] is True
    with zipfile.ZipFile(output / 'Karry-mobile-builds.zip') as archive:
        assert 'verification/.well-known/karry-agent-verification.txt' in archive.namelist()
        assert 'verification/dns.txt' in archive.namelist()
        assert b'NOT Apple signing' in archive.read('app-verification.txt')
        assert not any(name.endswith('.ipa') for name in archive.namelist())


def test_existing_output_is_never_overwritten(tmp_path):
    sentinel = tmp_path / 'precious.txt'
    sentinel.write_text('keep')
    with pytest.raises(SystemExit):
        release.main(['--platform', 'prepare', '--output', str(tmp_path)])
    assert sentinel.read_text() == 'keep'


def test_ios_is_explicitly_blocked_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(release.sys, 'platform', 'linux')
    output = tmp_path / 'build'
    assert release.main(['--platform', 'ios', '--output', str(output)]) == 2
    result = json.loads((output / 'result.json').read_text())
    assert result['status'] == 'incomplete'
    assert result['ios']['status'] == 'blocked'


def test_unsigned_cannot_generate_installable_ota(tmp_path):
    result = release.prepare_ota(tmp_path, {'status': 'succeeded', 'mode': 'unsigned'},
                                 'https://example.com/downloads', 'com.kishi.karryagent2')
    assert result['status'] == 'blocked'
    assert not (tmp_path / 'ota/manifest.plist').exists()


def fake_ipa(tmp_path, bundle='com.kishi.karryagent2', signed=True):
    path = tmp_path / 'test.ipa'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Payload/Test.app/Info.plist', plistlib.dumps({
            'CFBundleIdentifier': bundle, 'CFBundleVersion': '7', 'CFBundleDisplayName': 'Test'}))
        if signed:
            archive.writestr('Payload/Test.app/embedded.mobileprovision', 'unit-test-fixture-not-a-real-profile')
            archive.writestr('Payload/Test.app/_CodeSignature/CodeResources', 'unit-test-fixture')
    return path


def test_ota_manifest_uses_exported_app_metadata(tmp_path):
    fake_ipa(tmp_path)
    result = release.prepare_ota(tmp_path, {'status': 'succeeded', 'mode': 'adhoc', 'artifacts': ['test.ipa']},
                                 'https://example.com/ota', 'com.kishi.karryagent2')
    assert result['status'] == 'prepared_not_published'
    manifest = plistlib.loads((tmp_path / 'ota/manifest.plist').read_bytes())
    assert manifest['items'][0]['metadata']['bundle-version'] == '7'
    assert manifest['items'][0]['assets'][0]['url'] == 'https://example.com/ota/test.ipa'
    assert 'itms-services:' in (tmp_path / 'ota/install.html').read_text()


@pytest.mark.parametrize('bundle,signed', [('wrong.bundle', True), ('com.kishi.karryagent2', False)])
def test_ota_rejects_mismatched_or_unsigned_payload(tmp_path, bundle, signed):
    fake_ipa(tmp_path, bundle, signed)
    with pytest.raises(ValueError):
        release.prepare_ota(tmp_path, {'status': 'succeeded', 'mode': 'adhoc', 'artifacts': ['test.ipa']},
                            'https://example.com/ota', 'com.kishi.karryagent2')


def test_package_excludes_unlisted_files_and_rejects_external_artifacts(tmp_path):
    release.prepare_verification(tmp_path, None, None)
    (tmp_path / 'do-not-package.p12').write_bytes(b'not-a-certificate')
    path = release.package(tmp_path, {})
    with zipfile.ZipFile(path) as archive:
        assert 'do-not-package.p12' not in archive.namelist()
    with pytest.raises(ValueError):
        release.artifact_path(tmp_path, '../outside')
    (tmp_path / 'external').symlink_to(Path('/etc/hosts'))
    with pytest.raises(ValueError):
        release.artifact_path(tmp_path, 'external')


def test_failed_build_still_reports_partial_results(tmp_path, monkeypatch):
    def fail(*_args):
        raise ValueError('SDK unavailable')
    monkeypatch.setattr(release, 'build_android', fail)
    output = tmp_path / 'build'
    assert release.main(['--platform', 'android', '--output', str(output)]) == 2
    assert json.loads((output / 'result.json').read_text())['android']['status'] == 'failed'
    assert (output / 'Karry-mobile-builds.zip').is_file()


def test_agent4_uses_distinct_default_bundle_id(tmp_path, monkeypatch):
    captured = []

    def fake_build(_output, args):
        captured.append(args.bundle_id)
        return {'status': 'blocked', 'artifacts': []}

    monkeypatch.setattr(release, 'build_ios', fake_build)
    assert release.main(['--platform', 'ios', '--ios-scheme', 'KarryOperator4', '--output', str(tmp_path)]) == 2
    assert captured == ['com.kishi.karryagent4']


def test_android_install_failure_preserves_built_apks(tmp_path, monkeypatch):
    sdk = tmp_path / 'sdk'
    (sdk / 'build-tools/35.0.0').mkdir(parents=True)
    root = tmp_path / 'repo'
    for flavor in release.FLAVORS:
        apk = root / f'android-observer/app/build/outputs/apk/{flavor}/debug/app-{flavor}-debug.apk'
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b'fake-test-apk')
    monkeypatch.setattr(release, 'ROOT', root)
    monkeypatch.setattr(release, 'android_sdk', lambda: sdk)
    monkeypatch.setattr(release, 'run', lambda *args, **kwargs: None)

    def fake_output(command, **kwargs):
        if 'devices' in command:
            return 'List of devices attached\n'
        flavor = 'agent3' if 'agent3' in command[-1] else 'agent1'
        return f"package: name='{release.FLAVORS[flavor]}'"

    monkeypatch.setattr(release.subprocess, 'check_output', fake_output)
    result = release.build_android(tmp_path, list(release.FLAVORS), True)
    assert result['status'] == 'failed'
    assert result['emulator_installed'] is False
    assert len(result['artifacts']) == 2
    assert all((tmp_path / path).is_file() for path in result['artifacts'])
