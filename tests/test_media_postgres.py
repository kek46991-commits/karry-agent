import os
import uuid
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import media_repository as db, media_generation as gen, image_attachments as images
from test_image_attachments import picture

HEADERS = {'Authorization': 'Bearer synthetic-media-owner'}


@pytest.fixture
def database(monkeypatch):
    url = os.getenv('KARRY_TEST_DATABASE_URL')
    if not url:
        pytest.skip('isolated migrated PostgreSQL is not configured')
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv('KARRY_API_TOKEN', 'synthetic-media-owner')
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    monkeypatch.setenv('KARRY_MEDIA_GENERATION_ENABLED', '1')
    for name in ['_SESSIONS','_SESSION_META','_SESSION_LOCKS','_RUNS','_PRIVATE_SESSIONS']:
        monkeypatch.setattr(web, name, {})
    # Only an explicitly named TEST database is allowed to be reset.
    with psycopg.connect(url) as conn:
        assert 'test' in conn.execute('SELECT current_database()').fetchone()[0]
        conn.execute('TRUNCATE media_files,media_generation_jobs')
    yield


@pytest.fixture
def client(database):
    return TestClient(web.app)


def session(client):
    return client.post('/api/sessions', headers=HEADERS, json={}).json()['session_id']


def upload(client, sid):
    return client.post('/api/media/upload', headers=HEADERS, data={'session_id': sid}, files={'file': ('input.png', picture(), 'image/png')})


def test_actual_upload_insert_and_private_read(client):
    sid = session(client)
    response = upload(client, sid)
    assert response.status_code == 200
    row = response.json()
    assert row['type'] == 'image_input' and row['file_url'].startswith('data:image/png;base64,')
    assert row['created_at'] and uuid.UUID(row['id'])
    listing = client.get('/api/media', params={'session_id': sid}, headers=HEADERS)
    assert len(listing.json()) == 1 and 'base64' not in listing.text
    detail = client.get('/api/media/' + row['id'], params={'session_id': sid}, headers=HEADERS)
    assert detail.json()['file_url'] == row['file_url']
    assert upload(client, sid).json()['id'] == row['id']
    assert len(db.MediaRepository().list_media(sid)) == 1
    assert client.get('/api/media/' + row['id'], params={'session_id': session(client)}, headers=HEADERS).status_code == 404
    assert client.get('/api/media/' + row['id'], params={'session_id': sid}).status_code == 401


def test_database_type_constraint_is_real(database):
    repo = db.MediaRepository()
    with pytest.raises(db.MediaError):
        with repo.connection() as conn:
            conn.execute("INSERT INTO media_files(session_id,type,file_url) VALUES('test','not-allowed','x')")


def fake_provider(monkeypatch, status='processing'):
    provider = Mock()
    provider.create.return_value = {'id': 'prediction-test', 'status': status}
    provider.get.return_value = {'id': 'prediction-test', 'status': 'succeeded', 'output': ['https://replicate.delivery/output']}
    provider.output.return_value = images.normalize(picture(), 'image/png')
    monkeypatch.setattr(gen, 'ReplicateClient', lambda: provider)
    return provider


def generate(client, sid, request_id, prompt='A simple landscape'):
    return client.post('/api/media/generate', headers=HEADERS, json={'session_id': sid, 'prompt': prompt, 'request_id': str(request_id)})


def test_processing_is_not_success_and_duplicate_post_never_creates_twice(client, monkeypatch):
    sid, key = session(client), uuid.uuid4()
    provider = fake_provider(monkeypatch)
    first = generate(client, sid, key)
    assert first.status_code == 202 and first.json()['status'] == 'processing'
    assert db.MediaRepository().list_media(sid) == []
    assert generate(client, sid, key).json()['job_id'] == first.json()['job_id']
    provider.create.assert_called_once()
    assert generate(client, sid, key, 'Different').status_code == 409
    assert generate(client, sid, uuid.uuid4()).status_code == 409
    completed = client.get('/api/media/jobs/' + first.json()['job_id'], params={'session_id': sid}, headers=HEADERS)
    assert completed.json()['status'] == 'succeeded'
    row = db.MediaRepository().media(sid, uuid.UUID(completed.json()['media_id']))
    assert row['type'] == 'image_generated' and row['prompt'] == 'A simple landscape'
    assert row['file_url'].startswith('data:image/png;base64,')
    provider.create.assert_called_once()
    client.get('/api/media/jobs/' + first.json()['job_id'], params={'session_id': sid}, headers=HEADERS)
    provider.get.assert_called_once()


def test_create_timeout_is_persisted_and_never_reposted(client, monkeypatch):
    sid, key = session(client), uuid.uuid4()
    provider = fake_provider(monkeypatch)
    provider.create.side_effect = db.MediaError('unknown', 502, 'submission_unknown')
    first = generate(client, sid, key)
    assert first.status_code == 202 and first.json()['status'] == 'submission_unknown'
    assert generate(client, sid, key).json()['status'] == 'submission_unknown'
    provider.create.assert_called_once()
    assert db.MediaRepository().list_media(sid) == []


def test_generation_completed_during_create_is_copied_to_db(client, monkeypatch):
    sid, key = session(client), uuid.uuid4()
    provider = fake_provider(monkeypatch, 'succeeded')
    response = generate(client, sid, key)
    assert response.status_code == 200 and response.json()['status'] == 'succeeded'
    assert len(db.MediaRepository().list_media(sid)) == 1
    provider.get.assert_not_called()


def test_failed_prediction_has_no_image_row(client, monkeypatch):
    sid = session(client)
    provider = fake_provider(monkeypatch, 'failed')
    response = generate(client, sid, uuid.uuid4())
    assert response.json()['status'] == 'failed'
    assert db.MediaRepository().list_media(sid) == []
    provider.output.assert_not_called()


def test_generation_requires_explicit_enablement_and_known_task(client, monkeypatch):
    sid = session(client)
    factory = Mock(side_effect=AssertionError('no provider call'))
    monkeypatch.setattr(gen, 'ReplicateClient', factory)
    monkeypatch.setenv('KARRY_MEDIA_GENERATION_ENABLED', '0')
    assert generate(client, sid, uuid.uuid4()).status_code == 503
    assert generate(client, 'missing', uuid.uuid4()).status_code == 404
    factory.assert_not_called()


def test_output_save_failure_keeps_prediction_id_and_does_not_recreate(client, monkeypatch):
    sid, key = session(client), uuid.uuid4()
    provider = fake_provider(monkeypatch)
    first = generate(client, sid, key).json()
    provider.output.side_effect = db.MediaError('download failed', 502, 'output_download')
    assert client.get('/api/media/jobs/' + first['job_id'], params={'session_id': sid}, headers=HEADERS).status_code == 502
    assert db.MediaRepository().job(sid, uuid.UUID(first['job_id']))['state'] == 'processing'
    assert generate(client, sid, key).json()['job_id'] == first['job_id']
    provider.create.assert_called_once()


def test_generation_slot_is_reserved_against_uploads(client, monkeypatch):
    sid = session(client)
    fake_provider(monkeypatch)
    monkeypatch.setattr(db, 'MAX_FILES', 1)
    job = generate(client, sid, uuid.uuid4()).json()
    assert upload(client, sid).status_code == 409
    result = client.get('/api/media/jobs/' + job['job_id'], params={'session_id': sid}, headers=HEADERS)
    assert result.json()['status'] == 'succeeded'
    assert len(db.MediaRepository().list_media(sid)) == 1


def test_known_submission_rejection_releases_active_job(client, monkeypatch):
    sid = session(client)
    provider = fake_provider(monkeypatch)
    provider.create.side_effect = db.MediaError('rejected', 502, 'submission_rejected')
    key = uuid.uuid4()
    result = generate(client, sid, key)
    assert result.status_code == 200 and result.json()['status'] == 'failed'
    assert generate(client, sid, key).json()['status'] == 'failed'
    provider.create.assert_called_once()
    provider.create.side_effect = None
    assert generate(client, sid, uuid.uuid4()).json()['status'] == 'processing'
