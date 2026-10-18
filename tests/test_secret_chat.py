import copy
import io
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import types

import app as web
from karry_agent import secret_chat, secret_runtime, task_persistence, media_repository
from karry_agent.core import AgentCore, AgentMetrics, AgentResult, ExecutionEvent
from karry_agent.self_healing import SelfHealingEngine
from test_task_persistence import FakeS3
from karry_agent.website_storage import StorageConfig

HEADERS = {'Authorization': 'Bearer synthetic-secret-owner'}
SENTINEL = 'private-conversation-sentinel-971'
SID = 'a' * 32


class FakeAgent:
    def __init__(self):
        self.secret_calls = []
        self.normal_calls = []
        self.fail = False

    def run_secret(self, prompt, private_mode=False):
        self.secret_calls.append((prompt, private_mode))
        logging.getLogger('secret-test-provider').error('provider payload: %s', prompt)
        if self.fail:
            raise RuntimeError('Provider private details: ' + prompt)
        return AgentResult(text='secret reply ' + SENTINEL, steps=1, tool_calls=0,
            metrics=AgentMetrics(api_calls=1), trace=[ExecutionEvent(1, 'must-not-retain', {'prompt': prompt}, SENTINEL, 0)])

    def run(self, prompt, private_mode=False):
        self.normal_calls.append(prompt)
        logging.getLogger('secret-test-provider').info('normal request log')
        return AgentResult(text='normal reply', steps=1, tool_calls=0)


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', 'synthetic-secret-owner')
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    monkeypatch.setattr(secret_chat, 'store', secret_chat.SecretChatStore())
    for name in ['_RUNS', '_SESSIONS', '_PRIVATE_SESSIONS', '_PENDING_CALENDAR', '_SESSION_META', '_SESSION_LOCKS', '_SESSION_APPROVALS']:
        monkeypatch.setattr(web, name, {})
    agent = FakeAgent()
    old = dict(web.app.dependency_overrides)
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    with TestClient(web.app, raise_server_exceptions=False) as client:
        yield client, agent, monkeypatch
    web.app.dependency_overrides.clear()
    web.app.dependency_overrides.update(old)


def send(client, **fields):
    return client.post('/api/chat', headers=HEADERS, json={'session_id': 'normal-task', 'prompt': SENTINEL,
        'is_secret': True, 'secret_session_id': SID, **fields})


@pytest.mark.parametrize('path', ['/api/chat', '/chat', '/run', '/agent/run'])
def test_secret_skips_all_persistence_and_normal_caches(environment, path):
    client, agent, patch = environment
    patch.setenv('KARRY_TASK_STORAGE', 's3')
    def forbidden(*args, **kwargs):
        pytest.fail('Secret touched a persistent repository')
    patch.setattr(task_persistence, 'repository', forbidden)
    patch.setattr(media_repository, 'MediaRepository', forbidden)
    data = {'session_id': 'normal-task', 'isSecret': True, 'secret_session_id': SID,
            'task' if path == '/agent/run' else 'prompt': SENTINEL}
    response = client.post(path, headers=HEADERS, json=data)
    assert response.status_code == 200
    body = response.json()
    assert body['isSecret'] is True and body['is_secret'] is True
    assert body['secret_session_id'] == SID and body['trace'] == []
    assert len(agent.secret_calls) == 1 and agent.normal_calls == []
    assert web._RUNS == {} and web._SESSIONS == {} and web._PRIVATE_SESSIONS == {}
    assert 'no-store' in response.headers['cache-control']


def test_typescript_aliases_match_existing_api(environment):
    client, agent, _ = environment
    response = client.post('/api/chat', headers=HEADERS, json={'message': SENTINEL,
        'taskId': 'normal-task', 'isSecret': True})
    assert response.status_code == 200 and response.json()['persistence'] == 'none'
    assert len(response.json()['secret_session_id']) == 32


def test_normal_r2_history_unchanged_by_secret_then_normal_send(environment):
    client, agent, patch = environment
    patch.setenv('KARRY_TASK_STORAGE', 's3')
    patch.setenv('KARRY_TASK_S3_PREFIX', 'karry-tasks/')
    s3 = FakeS3()
    cfg = StorageConfig('private-bucket', 'karry-websites/', None, 'us-east-1', 'synthetic-access', 'synthetic-secret')
    repo = task_persistence.S3TaskRepository(cfg, s3)
    patch.setattr(task_persistence, 'repository', lambda: repo)
    repo.create('normal-task', 'Normal task')
    before = copy.deepcopy(s3.objects)
    assert send(client).status_code == 200
    assert before == s3.objects and SENTINEL not in str(s3.objects)
    normal = client.post('/api/chat', headers=HEADERS, json={'session_id': 'normal-task', 'prompt': 'public task message', 'isSecret': False})
    assert normal.status_code == 200 and normal.json()['isSecret'] is False
    saved = repo.load('normal-task')
    assert [m['content'] for m in saved['messages']] == ['public task message', 'normal reply']
    assert SENTINEL not in json.dumps(saved)
    assert len(agent.normal_calls) == 1 and SENTINEL not in agent.normal_calls[0]


def test_secret_context_and_private_persona_are_isolated(environment):
    client, agent, patch = environment
    patch.setenv('SECRET_PASSPHRASE', 'synthetic-private-mode')
    assert send(client, prompt='synthetic-private-mode ' + SENTINEL).status_code == 200
    assert send(client, prompt='follow up').status_code == 200
    assert SENTINEL in agent.secret_calls[1][0] and agent.secret_calls[1][1] is True
    second = send(client, secret_session_id='b' * 32, prompt='other conversation')
    assert second.status_code == 200 and SENTINEL not in agent.secret_calls[2][0]
    assert web._PRIVATE_SESSIONS == {}
    assert send(client, session_id='another-task', prompt='not allowed').status_code == 409


def test_secret_replay_never_calls_model_twice_and_rejects_changed_input(environment):
    client, agent, _ = environment
    first = send(client, request_id='same-request').json()
    second = send(client, request_id='same-request').json()
    assert first['run_id'] == second['run_id'] and second['replayed'] is True
    assert len(agent.secret_calls) == 1 and len(second['messages']) == 2
    assert send(client, request_id='same-request', prompt='different input').status_code == 409


def test_failure_has_no_content_or_healing_log_and_can_retry(environment, caplog):
    client, agent, patch = environment
    healer = SelfHealingEngine(cooldown=0)
    healer.set_patch_hook(lambda incident: pytest.fail('Secret queued a repair patch'))
    patch.setattr(web, 'healing_engine', healer)
    agent.fail = True
    with caplog.at_level(logging.DEBUG):
        response = send(client, request_id='failed-request')
    assert response.status_code == 502
    assert SENTINEL not in response.text and 'Provider' not in response.text and SENTINEL not in caplog.text
    assert healer.error_logs == [] and secret_chat.store._sessions[SID].history == []
    agent.fail = False
    assert send(client, request_id='failed-request').status_code == 200
    assert len(agent.secret_calls) == 2


def test_stream_only_returns_private_reply_to_caller_not_trace_or_cache(environment):
    client, agent, _ = environment
    response = send(client, stream=True)
    assert response.status_code == 200
    started = response.text.split('\n\n')[0]
    assert SENTINEL not in started and 'event: completed' in response.text
    assert 'event: step' not in response.text and 'must-not-retain' not in response.text
    assert web._RUNS == {} and 'no-store' in response.headers['cache-control']
    agent.fail = True
    failed = send(client, secret_session_id='c' * 32, stream=True)
    assert 'event: error' in failed.text and SENTINEL not in failed.text and 'Provider' not in failed.text


@pytest.mark.parametrize('fields', [
    {'is_secret': True, 'isSecret': False}, {'is_secret': 'true'},
    {'is_secret': False, 'secret_session_id': SID}, {'secret_session_id': '../secret'},
])
def test_invalid_flags_or_nonce_rejected_without_model(environment, fields):
    client, agent, _ = environment
    response = send(client, **fields)
    assert response.status_code == 422 and SENTINEL not in response.text and not agent.secret_calls


def test_unauthorized_and_images_do_not_call_model(environment):
    client, agent, _ = environment
    response = client.post('/api/chat', json={'prompt': SENTINEL, 'session_id': 'task', 'isSecret': True})
    assert response.status_code == 401 and SENTINEL not in response.text
    assert send(client, image_ids=['d' * 32]).status_code == 400
    assert not agent.secret_calls


def test_secret_forget_is_memory_only_and_identifier_not_in_url(environment):
    client, agent, patch = environment
    assert send(client).status_code == 200
    patch.setattr(task_persistence, 'repository', lambda: pytest.fail('forget touched R2'))
    response = client.post('/api/secret/forget', headers=HEADERS, json={'secret_session_id': SID})
    assert response.status_code == 200 and SID not in response.text
    assert SID not in secret_chat.store._sessions
    assert send(client, prompt='fresh').status_code == 200
    assert SENTINEL not in agent.secret_calls[-1][0]


def test_scope_suppresses_errors_and_restores_normal_logs(caplog):
    engine = SelfHealingEngine(cooldown=0)
    with caplog.at_level(logging.INFO):
        with secret_runtime.scope():
            logging.getLogger('scope-test').error(SENTINEL)
            assert engine.analyze_and_heal(RuntimeError(SENTINEL))['status'] == 'skipped'
        logging.getLogger('scope-test').info('normal logging is preserved')
    assert SENTINEL not in caplog.text and 'normal logging is preserved' in caplog.text
    assert engine.error_logs == []


def test_invalid_secret_body_skips_input_and_diagnostic_log(environment, caplog):
    client, agent, patch = environment
    engine = SelfHealingEngine()
    patch.setattr(web, 'healing_engine', engine)
    response = client.post('/api/chat', headers={**HEADERS, 'Content-Type': 'application/json'},
        content='{"isSecret":true,"prompt":"' + SENTINEL + '",')
    assert response.status_code == 422 and SENTINEL not in response.text and engine.error_logs == []
    assert not agent.secret_calls


def test_secret_store_limits_expiry_active_lock_and_copies():
    now = [0]
    store = secret_chat.SecretChatStore(clock=lambda: now[0])
    with store.claim('task', SID) as (_, entry):
        with pytest.raises(secret_chat.SecretChatError):
            with store.claim('task', SID):
                pass
        reply = entry.complete('id', 'private', {'text': 'reply'})
        reply['messages'][0]['content'] = 'modified'
        assert entry.history[0]['content'] == 'private'
    now[0] = secret_chat.TTL_SECONDS + 1
    with store.claim('task', SID) as (_, fresh):
        assert fresh.history == []


def test_real_core_uses_actual_gemini_text_request_without_tools(monkeypatch):
    calls = []
    response = SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role='model', parts=[types.Part(text='safe reply')]))],
        usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=3, total_token_count=5))
    def generate(**kwargs):
        calls.append(kwargs)
        return response
    agent = AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    result = agent.run_secret(SENTINEL)
    assert result.text == 'safe reply' and result.metrics.total_tokens == 5
    assert len(calls) == 1 and calls[0]['config'].tools == []
    assert 'シークレット' in calls[0]['config'].system_instruction
    assert result.trace == [] and result.tool_calls == 0 and not secret_runtime.is_active()


def test_parallel_normal_request_keeps_logs_and_history(environment, caplog):
    client, agent, patch = environment
    normal = client.post('/api/sessions', headers=HEADERS, json={'title': 'Normal task'}).json()['session_id']
    entered, release = threading.Event(), threading.Event()
    original = agent.run_secret
    def block(prompt, private_mode=False):
        assert secret_runtime.is_active()
        entered.set()
        assert release.wait(5)
        return original(prompt, private_mode)
    patch.setattr(agent, 'run_secret', block)
    responses = []
    with caplog.at_level(logging.INFO):
        thread = threading.Thread(target=lambda: responses.append(send(client)))
        thread.start()
        try:
            assert entered.wait(5)
            response = client.post('/api/chat', headers=HEADERS, json={'session_id': normal, 'prompt': 'normal parallel message'})
            assert response.status_code == 200 and 'normal request log' in caplog.text
        finally:
            release.set()
            thread.join(5)
    assert responses[0].status_code == 200 and SENTINEL not in caplog.text
    history = client.get('/sessions/' + normal, headers=HEADERS).json()['history']
    assert [row['content'] for row in history] == ['normal parallel message', 'normal reply']


def test_store_capacity_and_busy_forget_have_no_persistent_fallback(monkeypatch):
    monkeypatch.setattr(secret_chat, 'MAX_SESSIONS', 1)
    now = [0]
    store = secret_chat.SecretChatStore(clock=lambda: now[0])
    with store.claim('task', SID):
        with pytest.raises(secret_chat.SecretChatError):
            store.forget(SID)
        now[0] = secret_chat.TTL_SECONDS + 1
        with pytest.raises(secret_chat.SecretChatError):
            with store.claim('task', 'b' * 32):
                pass
    now[0] += secret_chat.TTL_SECONDS + 1
    with store.claim('task', 'b' * 32):
        assert SID not in store._sessions


def test_secret_model_function_call_is_rejected_without_tool_execution(monkeypatch):
    response = SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role='model',
        parts=[types.Part(function_call=types.FunctionCall(name='write_file', args={'path': 'secret.txt', 'content': SENTINEL}))]))])
    agent = AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw: response)))
    monkeypatch.setattr(agent.registry, 'execute', lambda *a, **k: pytest.fail('Secret executed a tool'))
    with pytest.raises(ValueError):
        agent.run_secret(SENTINEL)
    assert not secret_runtime.is_active()


def test_secret_core_provider_error_never_reaches_healing_hook_or_logs(caplog):
    engine = SelfHealingEngine()
    engine.set_patch_hook(lambda incident: pytest.fail('Secret called healing hook'))
    def failure(**kw):
        logging.getLogger('sdk-test').error('SDK failure %s', SENTINEL)
        raise RuntimeError(SENTINEL)
    agent = AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=failure)), healer=engine)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        agent.run_secret(SENTINEL)
    assert engine.error_logs == [] and SENTINEL not in caplog.text
    assert not secret_runtime.is_active()


@pytest.mark.parametrize('key,values', [
    ('isSecret', 'true,"isSecret":false'), ('isSecret', 'false,"isSecret":true'),
    ('is_secret', 'true,"is_secret":false'), ('is_secret', 'true,"is_secret":true'),
])
def test_duplicate_privacy_keys_never_reach_normal_storage(environment, key, values):
    client, agent, patch = environment
    patch.setenv('KARRY_TASK_STORAGE', 's3')
    patch.setattr(task_persistence, 'repository', lambda: pytest.fail('Duplicate flag touched R2'))
    body = '{"session_id":"normal-task","prompt":"' + SENTINEL + '","' + key + '":' + values + '}'
    result = client.post('/api/chat', headers={**HEADERS, 'Content-Type': 'application/json'}, content=body)
    assert result.status_code == 400 and SENTINEL not in result.text
    assert not agent.secret_calls and not agent.normal_calls and web._RUNS == {}


def test_lifespan_reaps_expired_secret_without_next_request(environment):
    _, agent, patch = environment
    now = [0]
    store = secret_chat.SecretChatStore(clock=lambda: now[0])
    patch.setattr(secret_chat, 'store', store)
    patch.setattr(secret_chat, 'CLEANUP_INTERVAL_SECONDS', 0.01)
    with TestClient(web.app, raise_server_exceptions=False) as client:
        assert send(client).status_code == 200 and SID in store._sessions
        now[0] = secret_chat.TTL_SECONDS + 1
        deadline = time.monotonic() + 2
        while SID in store._sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        assert SID not in store._sessions
        assert len(agent.secret_calls) == 1


def test_lifespan_discards_ram_on_shutdown(environment):
    _, agent, patch = environment
    store = secret_chat.SecretChatStore()
    patch.setattr(secret_chat, 'store', store)
    before = set(threading.enumerate())
    with TestClient(web.app, raise_server_exceptions=False) as client:
        assert send(client).status_code == 200 and SID in store._sessions
    assert store._sessions == {}
    assert not [t for t in set(threading.enumerate()) - before if t.name == 'secret-chat-expiry']
