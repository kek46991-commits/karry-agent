import json

import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent.core import AgentMetrics, AgentResult, ExecutionEvent
from karry_agent.secret_masking import mask_secrets, mask_text, mask_token


@pytest.mark.parametrize('raw, masked', [
    ('key sk-abcdefghijklmnopqrstuvwxyz9876', 'key sk-...9876'),
    ('sk-ant-api03-abcdefghijklmnopqrstu-XyZ1', 'sk-ant-...XyZ1'),
    ('AIzaSyA1234567890abcdefghijklmnopqrstu', 'AIza...rstu'),
    ('ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'ghp_...6789'),
    ('github_pat_11ABCDEFG0123456789_abcdefghijklmnop', 'github_pat_...mnop'),
    ('rnd_abcdefghijklmnopqrstuv', 'rnd_...stuv'),
    ('postgresql://user:p%40ss@host:5432/db', 'postgresql://[REDACTED]@host:5432/db'),
    ('Authorization: Bearer abcdefghijklmnopqrstuvwxyz', 'Authorization: Bearer abc...wxyz'),
])
def test_known_credential_formats_are_masked(raw, masked):
    assert mask_text(raw) == masked


def test_configured_env_secrets_are_masked_but_personal_context_is_not(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', 'short-tk')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://owner:verysecret@db.internal/prod')
    monkeypatch.setenv('PARTNER_NAME', 'Taro')
    text = mask_text('token short-tk dsn postgresql://owner:verysecret@db.internal/prod partner Taro')
    assert 'short-tk' not in text and 'verysecret' not in text and 'Taro' in text


def test_mask_secrets_hides_secret_fields_and_keeps_metrics():
    data = mask_secrets({'api_key': 'abc', 'arguments': {'password': 'x', 'query': 'ok'}, 'total_tokens': 5,
                         'next_page_token': 'page2', 'items': [('sk-' + 'a' * 20,)]})
    assert data == {'api_key': '[REDACTED]', 'arguments': {'password': '[REDACTED]', 'query': 'ok'},
                    'total_tokens': 5, 'next_page_token': 'page2', 'items': [('sk-...aaaa',)]}
    assert mask_token('short') == '[REDACTED]'


class LeakyAgent:
    def run(self, prompt, **kwargs):
        event = ExecutionEvent(1, 'read_file', {'path': '.env'},
                               {'content': 'GEMINI_API_KEY=AIzaSyA1234567890abcdefghijklmnopqrstu'}, 1.0)
        return AgentResult('鍵は sk-abcdefghijklmnopqrstuvwxyz9876 です', 1, 1, [event], AgentMetrics())


def test_chat_response_trace_and_stream_are_masked(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    monkeypatch.delenv('KARRY_API_TOKEN', raising=False)
    web.app.dependency_overrides[web.get_agent] = LeakyAgent
    try:
        client = TestClient(web.app)
        body = client.post('/chat', json={'prompt': 'show sk-abcdefghijklmnopqrstuvwxyz9876'}).json()
        stream = client.post('/chat', json={'prompt': 'show sk-abcdefghijklmnopqrstuvwxyz9876', 'stream': True}).text
        run = client.get(f"/runs/{body['run_id']}").json()
    finally:
        web.app.dependency_overrides.clear()
    for text in (json.dumps(body), stream, json.dumps(run)):
        assert 'abcdefghijklmnopqrstuvwxyz9876' not in text and '1234567890abcdefghij' not in text
    assert body['text'] == '鍵は sk-...9876 です'
    assert body['trace'][0]['observation']['content'].endswith('AIza...rstu')
