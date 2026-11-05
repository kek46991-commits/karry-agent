from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

import app as web
from karry_agent import core
from karry_agent.core import AgentCore

OWNER = 'synthetic-owner-' + 'token'
SECRET = 'synthetic-gemini-' + 'key'


def text_response(text):
    part = SimpleNamespace(text=text, function_call=None)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role='model', parts=[part]))])


def test_timeout_is_configurable_and_clamped(monkeypatch):
    monkeypatch.delenv('KARRY_GEMINI_TIMEOUT_MS', raising=False)
    assert core.gemini_timeout_ms() == 45_000
    for raw, expected in (('1000', 5_000), ('999999', 120_000), ('30000', 30_000), ('x', 45_000)):
        monkeypatch.setenv('KARRY_GEMINI_TIMEOUT_MS', raw)
        assert core.gemini_timeout_ms() == expected


def test_client_uses_the_timeout_and_reads_the_key_from_env(monkeypatch):
    seen = {}
    monkeypatch.setenv('GEMINI_API_KEY', SECRET)
    monkeypatch.setenv('KARRY_GEMINI_TIMEOUT_MS', '20000')
    monkeypatch.setattr(core.genai, 'Client', lambda **kw: seen.update(kw) or 'client')
    assert core.get_client() == 'client' and seen['api_key'] == SECRET and seen['http_options'].timeout == 20_000


def test_network_timeouts_are_retried_then_reported_as_failed():
    class Flaky:
        calls = 0

        def generate_content(self, **_):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise httpx.ReadTimeout('read timed out')
            return text_response('ok')
    agent = AgentCore(client=SimpleNamespace(models=Flaky()), sleep_fn=lambda _: None)
    assert agent.run('画像を見て').text == 'ok' and Flaky.calls == 2

    class Down:
        def generate_content(self, **_):
            raise httpx.ConnectError('network error')
    result = AgentCore(client=SimpleNamespace(models=Down()), sleep_fn=lambda _: None).run('画像を見て')
    assert result.status == 'failed' and 'Timeout' in result.text


def test_status_endpoint_is_owner_only_and_never_returns_the_key(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    http = TestClient(web.app)
    assert http.get('/api/gemini/status').status_code == 401
    monkeypatch.setenv('GEMINI_API_KEY', SECRET)
    response = http.get('/api/gemini/status', headers={'Authorization': 'Bearer ' + OWNER})
    assert response.status_code == 200 and response.json()['configured'] is True and SECRET not in response.text
    monkeypatch.setenv('GEMINI_API_KEY', 'your-gemini-api-key')
    assert http.get('/api/gemini/status', headers={'Authorization': 'Bearer ' + OWNER}).json()['configured'] is False
