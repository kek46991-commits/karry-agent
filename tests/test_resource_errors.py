from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app as web
from karry_agent import instant_website as sites, website_storage as storage, resource_errors as errors
from karry_agent import image_attachments as images, task_persistence as tasks

NAME = 'sample_' + 'a'*32 + '.html'
URL = '/static/instant/' + NAME
OWNER = {'Authorization': 'Bearer synthetic-resource-owner'}


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv('KARRY_API_TOKEN', 'synthetic-resource-owner')
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    monkeypatch.setenv('KARRY_WEBSITE_STORAGE', 'local')
    for name in ['_RUNS', '_SESSIONS', '_SESSION_META', '_SESSION_LOCKS', '_PRIVATE_SESSIONS']:
        monkeypatch.setattr(web, name, {})
    sites.configure_static_root(tmp_path / 'static')
    sites.INSTANT_DIR.mkdir(parents=True)
    yield
    sites.configure_static_root(web.STATIC_DIR)


def test_missing_browser_artifact_is_helpful_html_not_raw_json():
    client = TestClient(web.app)
    response = client.get(URL, headers={'Accept': 'text/html'})
    assert response.status_code == 404 and response.headers['content-type'].startswith('text/html')
    assert '成果物が見つかりません' in response.text and 'チャット画面に戻る' in response.text
    assert 'href="/"' in response.text and 'artifact not found' not in response.text
    assert NAME not in response.text and '<script' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert "default-src 'none'" in response.headers['content-security-policy']
    assert client.get('/health').json()['status'] == 'ok'
    assert client.get('/').status_code == 200


@pytest.mark.parametrize('accept', ['application/json', 'application/json, */*'])
def test_missing_json_artifact_keeps_404_and_explicit_status(accept):
    response = TestClient(web.app).get(URL, headers={'Accept': accept})
    assert response.status_code == 404
    data = response.json()
    assert data['status'] == 'not_found' and data['error_code'] == 'artifact_not_found'
    assert data['recovery_url'] == '/' and '見つかりません' in data['detail']
    assert 'artifact not found' not in response.text


def test_head_missing_and_successful_artifacts_have_no_body():
    client = TestClient(web.app)
    get = client.get(URL, headers={'Accept': 'text/html'})
    head = client.head(URL, headers={'Accept': 'text/html'})
    assert head.status_code == 404 and head.content == b''
    assert head.headers['content-length'] == get.headers['content-length']
    body = '<!doctype html><title>Saved</title><h1>Saved</h1>'
    (sites.INSTANT_DIR / NAME).write_text(body)
    assert client.head(URL).status_code == 200 and client.head(URL).content == b''
    assert client.get(URL).text == body
    assert 'sandbox allow-scripts' in client.get(URL).headers['content-security-policy']


@pytest.mark.parametrize('path', ['/static//instant/missing.html', '/static/instant/a/b.html', '/static/instant/.pending-private.tmp'])
def test_static_alias_and_unmanaged_paths_never_return_internal_json(path):
    response = TestClient(web.app).get(path, headers={'Accept': 'text/html'})
    assert response.status_code == 404 and 'artifact not found' not in response.text
    assert 'チャット画面に戻る' in response.text


def test_reading_after_file_disappears_returns_404_not_a_500(monkeypatch):
    path = sites.INSTANT_DIR / NAME
    monkeypatch.setattr(sites, 'public_artifact_content', lambda _: path)
    response = TestClient(web.app).get(URL, headers={'Accept': 'application/json'})
    assert response.status_code == 404 and response.json()['status'] == 'not_found'


def test_replacement_symlink_is_never_read(monkeypatch, tmp_path):
    target = tmp_path / 'private-source'; target.write_text('private-source-secret')
    path = sites.INSTANT_DIR / NAME; path.symlink_to(target)
    monkeypatch.setattr(sites, 'public_artifact_content', lambda _: path)
    response = TestClient(web.app).get(URL, headers={'Accept': 'application/json'})
    assert response.status_code == 404 and 'private-source-secret' not in response.text


@pytest.mark.parametrize('fault', [storage.StorageError('private-signature-url', 'storage_unavailable'), PermissionError('private-file-path'), UnicodeError('private-content')])
def test_storage_outage_is_503_and_never_claims_deleted_or_success(monkeypatch, fault):
    monkeypatch.setattr(sites, 'public_artifact_content', Mock(side_effect=fault))
    client = TestClient(web.app)
    for accept in ['application/json', 'text/html']:
        response = client.get(URL, headers={'Accept': accept})
        assert response.status_code == 503 and 'private-' not in response.text
        assert '現在' in response.text and response.headers['cache-control'] == 'no-store'
    assert client.get('/health').status_code == 200


def test_unexpected_retrieval_error_is_safe_500_and_app_remains_alive(monkeypatch):
    monkeypatch.setattr(sites, 'public_artifact_content', Mock(side_effect=RuntimeError('private-debug-trace')))
    client = TestClient(web.app)
    response = client.get(URL, headers={'Accept': 'application/json'})
    assert response.status_code == 500 and response.json()['status'] == 'error'
    assert 'private-debug-trace' not in response.text
    assert client.get('/health').status_code == 200


def test_http_not_found_is_not_recaught_as_internal_error(monkeypatch):
    monkeypatch.setattr(sites, 'public_artifact_content', Mock(side_effect=HTTPException(404, 'private-detail')))
    response = TestClient(web.app).get(URL, headers={'Accept': 'application/json'})
    assert response.status_code == 404 and 'private-detail' not in response.text


@pytest.mark.parametrize('path', ['/api/sessions/missing', '/sessions/missing', '/api/images/'+'a'*32+'?session_id=missing', '/runs/missing'])
def test_missing_private_resources_require_auth_and_use_japanese_json(path):
    client = TestClient(web.app)
    assert client.get(path).status_code == 401
    response = client.get(path, headers={**OWNER, 'Accept': 'text/html'})
    assert response.status_code == 404 and response.headers['content-type'].startswith('application/json')
    assert response.json()['status'] == 'not_found' and '見つかりません' in response.json()['detail']
    assert response.headers['cache-control'] == 'no-store'


def test_r2_task_not_found_preserves_existing_machine_code(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE', 's3')
    repo = Mock(); repo.reconcile.side_effect = tasks.TaskStorageError('Session not found', 'not_found')
    monkeypatch.setattr(tasks, 'repository', lambda: repo)
    response = TestClient(web.app).get('/api/sessions/missing', headers=OWNER)
    assert response.status_code == 404 and response.json()['error_code'] == 'not_found'
    assert 'タスク' in response.json()['detail']


def test_global_unknown_retrieval_error_has_safe_json(monkeypatch):
    monkeypatch.setattr(web, '_session_detail_locked', Mock(side_effect=RuntimeError('private-state')))
    response = TestClient(web.app, raise_server_exceptions=False).get('/api/sessions/missing', headers=OWNER)
    assert response.status_code == 500 and response.json()['status'] == 'error'
    assert 'private-state' not in response.text


def test_empty_or_malformed_local_artifact_is_unavailable_not_success():
    (sites.INSTANT_DIR / NAME).write_bytes(b'')
    response = TestClient(web.app).get(URL, headers={'Accept': 'application/json'})
    assert response.status_code == 503 and response.json()['status'] == 'unavailable'


@pytest.mark.parametrize('status', [400,401,403,409,500,503])
def test_http_exceptions_keep_status_without_leaking_internal_detail(monkeypatch, status):
    monkeypatch.setattr(sites, 'public_artifact_content', Mock(side_effect=HTTPException(status, 'provider private endpoint https://secret.example/path', headers={'Retry-After':'10'})))
    client = TestClient(web.app)
    for accept in ['text/html', 'application/json']:
        response = client.get(URL, headers={'Accept':accept})
        assert response.status_code == status and 'secret.example' not in response.text
        assert response.headers['retry-after']=='10' and response.headers['cache-control']=='no-store'
        assert response.headers['content-type'].startswith('text/html' if accept=='text/html' else 'application/json')


def test_artifact_status_is_explicit_and_safe_when_storage_is_unavailable(monkeypatch):
    client=TestClient(web.app)
    assert client.get('/api/artifacts/'+NAME).status_code==401
    missing=client.get('/api/artifacts/'+NAME, headers=OWNER)
    assert missing.status_code==200 and missing.json()['published'] is False
    monkeypatch.setattr(sites, 'public_artifact_content', Mock(side_effect=storage.StorageError('private-signed-provider-url')))
    response=client.get('/api/artifacts/'+NAME, headers=OWNER)
    assert response.status_code==503 and response.json()['status']=='unavailable'
    assert 'private-signed-provider-url' not in response.text


def test_accept_json_preference_or_html_q_zero_is_respected():
    for accept in ['text/html;q=0,application/json', 'application/json;q=1,text/html;q=0.1']:
        response=TestClient(web.app).get(URL,headers={'Accept':accept})
        assert response.status_code==404 and response.headers['content-type'].startswith('application/json')
