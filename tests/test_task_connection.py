from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app as web
from karry_agent import task_persistence as tasks, website_storage as storage


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', 'test-owner-connection')
    monkeypatch.setenv('KARRY_TASK_STORAGE', 's3')
    web.app.dependency_overrides.clear()
    yield TestClient(web.app)
    web.app.dependency_overrides.clear()


def auth():
    return {'Authorization': 'Bearer test-owner-connection'}


def repo(client):
    config = storage.StorageConfig('private-test-bucket', 'websites/', 'https://'+'a'*32+'.r2.cloudflarestorage.com', 'auto', 'private-access-key', 'private-secret-key')
    return tasks.S3TaskRepository(config, client=client)


@pytest.mark.parametrize('provider,category', [
    ('NoSuchBucket', 'storage_bucket_missing'),
    ('AccessDenied', 'storage_permission'),
    ('InvalidAccessKeyId', 'storage_credentials'),
    ('SignatureDoesNotMatch', 'storage_signature'),
    ('PermanentRedirect', 'storage_endpoint'),
    ('NotImplemented', 'storage_unsupported'),
    ('new-private-provider-error', 'storage_unknown'),
])
def test_real_read_diagnostic_does_not_leak_provider_message(owner, monkeypatch, provider, category):
    client = Mock()
    client.get_object.side_effect = ClientError({'Error': {'Code': provider, 'Message': 'private-secret-key with signed-url'}}, 'GetObject')
    monkeypatch.setattr(tasks, 'repository', lambda: repo(client))
    response = owner.get('/api/agent/tasks/storage', headers=auth())
    assert response.status_code == 503
    data = response.json()
    assert data['configuration_valid'] is True and data['connection_status'] == 'failed'
    assert data['diagnostic_code'] == category and data['error_code'] == 'storage_unavailable'
    assert data['target']['bucket'] == 'private-test-bucket'
    assert response.headers['cache-control'] == 'no-store'
    assert all(secret not in response.text for secret in ['private-secret-key', 'private-access-key', 'signed-url', 'new-private-provider-error'])
    client.put_object.assert_not_called()


def test_connection_diagnostic_requires_auth_without_storage_access(owner, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(tasks, 'repository', factory)
    assert owner.get('/api/agent/tasks/storage').status_code == 401
    factory.assert_not_called()


def test_connection_diagnostic_checks_task_listing_not_just_config(owner, monkeypatch):
    factory = SimpleNamespace(config=repo(Mock()).config, prefix='karry-tasks/', list=Mock(return_value=[{'session_id': 'x', 'title': 'Task'}]))
    monkeypatch.setattr(tasks, 'repository', lambda: factory)
    response = owner.get('/api/agent/tasks/storage', headers=auth())
    assert response.status_code == 200
    assert response.json()['connection_status'] == 'connected' and response.json()['tasks_count'] == 1
    factory.list.assert_called_once()


def test_missing_config_is_not_a_connected_storage(owner, monkeypatch):
    monkeypatch.setattr(tasks, 'repository', Mock(side_effect=tasks.TaskStorageError('configure bucket', 'storage_configuration')))
    response = owner.get('/api/agent/tasks/storage', headers=auth())
    assert response.status_code == 503 and response.json()['configuration_valid'] is False


def test_legacy_task_error_code_and_new_hint_are_both_preserved(owner, monkeypatch):
    client = Mock()
    client.get_object.side_effect = ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'secret provider details'}}, 'GetObject')
    monkeypatch.setattr(tasks, 'repository', lambda: repo(client))
    response = owner.get('/api/sessions', headers=auth())
    assert response.status_code == 503
    assert response.json()['error_code'] == 'storage_unavailable'
    assert response.json()['diagnostic_code'] == 'storage_permission'
    assert '権限' in response.json()['hint'] and 'secret provider details' not in response.text


def test_memory_diagnostic_is_explicitly_ephemeral(owner, monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    response = owner.get('/api/agent/tasks/storage', headers=auth())
    assert response.status_code == 200 and response.json()['durable'] is False
    assert response.json()['connection_status'] == 'ephemeral'
