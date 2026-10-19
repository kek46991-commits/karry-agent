import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_task_persistence import FakeS3

import app as web
from karry_agent import knowledge_sync_service as ks
from karry_agent import task_persistence as tasks
from karry_agent import website_storage as websites
from karry_agent.core import AgentCore
from karry_agent.tools import ToolRegistry

OWNER = 'synthetic-owner-token'
URL = '/api/knowledge/sync-context'


def payload(**kw):
    body = {'context_type': 'system_instruction', 'source': 'Gemini-Master-Session',
            'content': '設計ルール: APIは必ず所有者認証を通す。', 'metadata': {'updated_by': 'Owner', 'priority': 'high'}}
    body.update(kw)
    return body


@pytest.fixture(autouse=True)
def memory(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    monkeypatch.setattr(ks, '_MEMORY', {})


@pytest.fixture
def client():
    return TestClient(web.app)


@pytest.mark.parametrize('headers, status', [
    ({}, 401),
    ({'X-Owner-Token': 'wrong'}, 401),
    ({'Authorization': 'Bearer wrong'}, 401),
    ({'Authorization': 'Basic ' + OWNER}, 401),
])
def test_sync_rejects_missing_or_wrong_owner_token(client, headers, status):
    assert client.post(URL, json=payload(), headers=headers).status_code == status
    assert ks._MEMORY == {}


def test_sync_fails_closed_without_configured_token(client, monkeypatch):
    monkeypatch.delenv('KARRY_API_TOKEN')
    assert client.post(URL, json=payload(), headers={'X-Owner-Token': 'anything'}).status_code == 503


@pytest.mark.parametrize('headers', [{'X-Owner-Token': OWNER}, {'Authorization': 'Bearer ' + OWNER}])
def test_sync_accepts_owner_header_or_bearer_and_updates_in_place(client, headers):
    first = client.post(URL, json=payload(), headers=headers)
    assert first.status_code == 201 and first.headers['cache-control'] == 'no-store'
    data = first.json()
    assert data['status'] == 'created' and data['context']['revision'] == 1
    assert data['active_contexts'] == {'conversation_context': 0, 'system_instruction': 1, 'global_knowledge': 0}
    second = client.post(URL, json=payload(content='設計ルール v2'), headers=headers)
    assert second.status_code == 200 and second.json()['status'] == 'updated'
    assert second.json()['context']['id'] == data['context']['id'] and second.json()['context']['revision'] == 2
    listed = client.get('/api/knowledge/contexts', headers=headers).json()['contexts']
    assert len(listed) == 1 and listed[0]['preview'] == '設計ルール v2' and 'content' not in listed[0]


@pytest.mark.parametrize('change', [
    {'context_type': 'unknown'}, {'content': ''}, {'content': 'x' * (ks.MAX_CONTENT_CHARS + 1)},
    {'source': '<script>'}, {'metadata': {'priority': 'urgent'}}, {'extra': 1},
])
def test_sync_validates_payload(client, change):
    assert client.post(URL, json=payload(**change), headers={'X-Owner-Token': OWNER}).status_code == 422


def test_sync_masks_credentials_before_saving(client, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_API_KEY', 'synthetic-configured-key-0001')
    content = ('Use sk-proj-abcdefghijklmnopqrstuvwxyz1234 and synthetic-configured-key-0001 '
               'with postgresql://admin:hunter2@db.internal/app')
    res = client.post(URL, json=payload(context_type='global_knowledge', content=content), headers={'X-Owner-Token': OWNER})
    assert res.status_code == 201 and res.json()['masked_secrets'] is True
    stored = json.dumps(ks._MEMORY, ensure_ascii=False)
    for secret in ('abcdefghijklmnopqrstuvwxyz1234', 'synthetic-configured-key-0001', 'hunter2'):
        assert secret not in stored and secret not in res.text
    assert 'sk-proj-...1234' in stored and 'postgresql://[REDACTED]@db.internal' in stored


def test_delete_context(client):
    headers = {'X-Owner-Token': OWNER}
    cid = client.post(URL, json=payload(), headers=headers).json()['context']['id']
    assert client.delete(f'/api/knowledge/contexts/{cid}', headers=headers).json()['status'] == 'deleted'
    assert client.delete(f'/api/knowledge/contexts/{cid}', headers=headers).status_code == 404
    assert client.delete('/api/knowledge/contexts/not-an-id', headers=headers).status_code == 422
    assert client.get('/api/knowledge/contexts').status_code == 401


def test_prompt_context_combines_instructions_conversation_and_rag():
    service = ks.KnowledgeSyncService()
    service.sync('system_instruction', 'Gemini-Master-Session', '回答の最後に必ず次のアクションを書く。', {'priority': 'high'})
    service.sync('conversation_context', 'Gemini-Master-Session', '現在はポートフォリオ公開の準備中。', {})
    service.sync('global_knowledge', 'design-notes', 'postgres の接続は pool を使う。', {})
    service.sync('global_knowledge', 'design-notes', 'frontend は React で実装する。', {})
    block = service.prompt_context('postgres の設定を教えて')
    assert block.startswith(ks.TRUST_NOTICE)
    data = json.loads(block.split('\n', 1)[1])['synced_context']
    assert data['system_instruction'][0]['content'] == '回答の最後に必ず次のアクションを書く。'
    assert data['conversation_context'][0]['content'] == '現在はポートフォリオ公開の準備中。'
    assert [row['content'] for row in data['global_knowledge']] == ['postgres の接続は pool を使う。']
    assert ks.KnowledgeSyncService().prompt_context('anything') != ''
    ks._MEMORY.clear()
    assert ks.KnowledgeSyncService().prompt_context('anything') == ''


def test_rag_uses_embeddings_when_available():
    def embedder(texts, task_type):
        return [[1.0 if 'cache' in t else 0.0, 1.0 if 'queue' in t else 0.0] + [0.0] * 254 for t in texts]
    service = ks.KnowledgeSyncService(embedder=embedder)
    service.sync('global_knowledge', 'notes', 'cache layer: redis', {})
    service.sync('global_knowledge', 'notes', 'queue: sqs', {})
    assert all(row['embedding'] for row in ks._MEMORY.values())
    data = json.loads(service.prompt_context('which cache?').split('\n', 1)[1])['synced_context']
    assert [row['content'] for row in data['global_knowledge']] == ['cache layer: redis']


def test_durable_store_uses_task_prefix(monkeypatch):
    monkeypatch.setenv('KARRY_TASK_STORAGE', 's3')
    monkeypatch.setenv('KARRY_TASK_S3_PREFIX', 'karry-tasks/')
    cfg = websites.StorageConfig('private-bucket', 'karry-websites/', None, 'us-east-1', 'synthetic-access', 'synthetic-secret')
    s3 = FakeS3()
    monkeypatch.setattr(tasks, 'repository', lambda: tasks.S3TaskRepository(cfg, s3))
    ks.KnowledgeSyncService().sync('system_instruction', 'src', 'rule', {})
    assert [k for k in s3.objects if 'knowledge' in k] == ['karry-tasks/knowledge/contexts.json']
    assert ks._MEMORY == {} and len(ks.KnowledgeContextStore().entries()) == 1
    s3.fail = True
    res = TestClient(web.app).post(URL, json=payload(), headers={'X-Owner-Token': OWNER})
    assert res.status_code == 503 and res.json()['error_code'] == 'storage_unavailable'


class RecordingModels:
    def __init__(self):
        self.configs = []

    def generate_content(self, **kwargs):
        self.configs.append(kwargs['config'])
        content = SimpleNamespace(role='model', parts=[SimpleNamespace(text='了解', function_call=None)])
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


def test_agent_run_uses_synced_context_immediately():
    client = SimpleNamespace(models=RecordingModels())
    agent = AgentCore(client=client, registry=ToolRegistry())
    agent.run('こんにちは')
    assert 'synced_context' not in client.models.configs[-1].system_instruction
    ks.KnowledgeSyncService().sync('system_instruction', 'Gemini-Master-Session', '最新ルール: 関西弁で答える。', {})
    agent.run('こんにちは')
    assert '最新ルール: 関西弁で答える。' in client.models.configs[-1].system_instruction


def test_agent_run_survives_sync_store_outage(monkeypatch):
    def broken(self):
        raise tasks.TaskStorageError('down', 'storage_unavailable')
    monkeypatch.setattr(ks.KnowledgeContextStore, 'entries', broken)
    client = SimpleNamespace(models=RecordingModels())
    assert AgentCore(client=client, registry=ToolRegistry()).run('こんにちは').text == '了解'
