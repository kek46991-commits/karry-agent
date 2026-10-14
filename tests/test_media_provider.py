from unittest.mock import Mock
import uuid

import pytest
import requests
from fastapi.testclient import TestClient

import app as web
from karry_agent import media_generation as gen, media_repository as db, image_attachments as images


def client(monkeypatch):
    monkeypatch.setenv('REPLICATE_API_TOKEN', 'synthetic-provider-token')
    return gen.ReplicateClient()


def test_official_model_route_not_invalid_version(monkeypatch):
    provider = client(monkeypatch)
    reply = Mock(status_code=201, content=b'{}')
    reply.json.return_value = {'id': 'prediction-test', 'status': 'processing', 'logs': 'must never be returned'}
    provider.api.post = Mock(return_value=reply)
    result = provider.create('A landscape')
    args, kwargs = provider.api.post.call_args
    assert args[0] == 'https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions'
    assert 'version' not in kwargs['json'] and kwargs['json']['input']['num_outputs'] == 1
    assert kwargs['allow_redirects'] is False and kwargs['timeout'] == (5, 20)
    assert provider.api.trust_env is False and result['status'] == 'processing'


def test_provider_errors_are_static_not_raw(monkeypatch):
    provider = client(monkeypatch)
    provider.api.post = Mock(return_value=Mock(status_code=401, content=b'synthetic-provider-token'))
    with pytest.raises(db.MediaError) as exc:
        provider.create('A landscape')
    assert 'synthetic-provider-token' not in str(exc.value)


@pytest.mark.parametrize('url', ['http://replicate.delivery/file', 'https://localhost/file', 'https://replicate.delivery.attacker.invalid/file', 'https://user:password@replicate.delivery/file', 'https://replicate.delivery:444/file'])
def test_download_url_is_allowlisted_without_any_api_header(monkeypatch, url):
    provider = client(monkeypatch)
    with pytest.raises(db.MediaError) as exc:
        provider.output({'output': [url]})
    assert exc.value.code == 'output_url'


def test_prediction_mismatch_rejected(monkeypatch):
    provider = client(monkeypatch)
    reply = Mock(status_code=200, content=b'{}')
    reply.json.return_value = {'id': 'other-prediction', 'status': 'succeeded'}
    provider.api.get = Mock(return_value=reply)
    with pytest.raises(db.MediaError):
        provider.get('wanted-prediction')


def test_provider_create_network_error_never_retries(monkeypatch):
    provider = client(monkeypatch)
    provider.api.post = Mock(side_effect=requests.Timeout('private request'))
    with pytest.raises(db.MediaError) as exc:
        provider.create('A landscape')
    provider.api.post.assert_called_once()
    assert exc.value.code == 'submission_unknown' and 'private request' not in str(exc.value)


def test_database_dsn_is_required_not_memory_fallback(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    with pytest.raises(db.MediaError) as exc:
        db.MediaRepository().list_media('task')
    assert exc.value.code == 'database_configuration'


def test_media_upload_guard_requires_owner_before_body_parse(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', 'synthetic-owner-key')
    response = TestClient(web.app).post('/api/media/upload', content=b'x'*(images.FORM_BODY_LIMIT+1))
    assert response.status_code == 401
    response = TestClient(web.app).post('/api/media/upload', content=b'x'*(images.FORM_BODY_LIMIT+1), headers={'Authorization': 'Bearer synthetic-owner-key'})
    assert response.status_code == 413


def test_download_uses_fresh_unauthenticated_session_and_copies_bytes(monkeypatch):
    from test_image_attachments import picture
    provider = client(monkeypatch)
    response = Mock(status_code=200, headers={'Content-Type': 'image/png'})
    response.raw.read1.side_effect = [picture(), b'']
    download = Mock()
    download.get.return_value = response
    manager = Mock()
    manager.__enter__ = Mock(return_value=download)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(gen.requests, 'Session', lambda: manager)
    output = provider.output({'output': ['https://replicate.delivery/output.png']})
    assert output.mime_type == 'image/png'
    assert download.get.call_args.kwargs['headers'] == {'Accept-Encoding': 'identity'}
    assert download.get.call_args.kwargs['allow_redirects'] is False
    response.close.assert_called_once()


def test_database_dsn_is_redacted_from_diagnostics(monkeypatch):
    from karry_agent.self_healing import redact
    value = 'postgresql://owner:synthetic-private-password@example.invalid/database'
    monkeypatch.setenv('DATABASE_URL', value)
    assert value not in redact('error: ' + value)
    assert redact({'DATABASE_URL': value})['DATABASE_URL'] == '[redacted]'
