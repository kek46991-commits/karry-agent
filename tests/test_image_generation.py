import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from google.genai import errors

import app as web
from fastapi.testclient import TestClient
from karry_agent import image_attachments as attachments, image_generation, task_persistence as tasks
from karry_agent.core import AgentCore
from test_image_attachments import HEADERS, create, durable, environment, picture  # noqa: F401


def gemini_image(body):
    part = SimpleNamespace(inline_data=SimpleNamespace(data=body, mime_type='image/png'), text=None)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])


def call(name, args):
    part = SimpleNamespace(text=None, thought=False, function_call=SimpleNamespace(name=name, args=args))
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role='model', parts=[part]))])


def text(value):
    part = SimpleNamespace(text=value, thought=False, function_call=None)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role='model', parts=[part]))])


def agent_requesting(count=1):
    replies = [call('generate_image', {'prompt': '夕焼けの富士山'}) for _ in range(count)] + [text('画像を作成しました。')]
    requests = []

    def generate(**kwargs):
        requests.append(kwargs)
        return replies[len(requests) - 1]
    return AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=generate)), max_retries=1), requests


def image_api(monkeypatch, result):
    prompts = []

    def generate(**kwargs):
        prompts.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(image_generation, '_client', lambda: SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    return prompts


def chat(client, sid, prompt='夕焼けの富士山の画像を作って'):
    return client.post('/api/chat', headers=HEADERS, json={'session_id': sid, 'prompt': prompt}).json()


def assert_visible_generated_image(client, sid, result):
    reply = result['messages'][-1]
    assert reply['role'] != 'user' and len(reply['images']) == 1
    image_id = reply['images'][0]
    assert result['image_assets'][image_id]['mime_type'] == 'image/jpeg'
    assert result['pending_images'] == []
    preview = client.get('/api/images/' + image_id, params={'session_id': sid}, headers=HEADERS)
    assert preview.status_code == 200
    assert preview.json()['image_url'].startswith('data:image/jpeg;base64,')
    assert base64.b64decode(preview.json()['image_url'].split(',', 1)[1])[:2] == b'\xff\xd8'
    return image_id


def test_tool_requires_chat_task_context():
    assert 'Web チャット' in image_generation.generate_image('猫')['error']


def test_chat_generates_image_and_attaches_it_to_reply(monkeypatch):
    prompts = image_api(monkeypatch, gemini_image(picture(size=(64, 48))))
    agent, requests = agent_requesting()
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    sid = create(client)
    result = chat(client, sid)
    assert result['status'] == 'completed'
    assert prompts[0]['model'] == 'gemini-2.5-flash-image'
    assert prompts[0]['config'].response_modalities == ['IMAGE']
    tool_result = str(requests[1]['contents'][-2].parts[0].function_response.response)
    assert 'success' in tool_result and '.png' not in tool_result
    image_id = assert_visible_generated_image(client, sid, result)
    detail = client.get('/api/sessions/' + sid, headers=HEADERS).json()
    assert detail['messages'][-1]['images'] == [image_id]


def test_durable_task_keeps_generated_image(monkeypatch, durable):
    image_api(monkeypatch, gemini_image(picture(size=(64, 48))))
    agent, _ = agent_requesting()
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    sid = create(client)
    image_id = assert_visible_generated_image(client, sid, chat(client, sid))
    monkeypatch.setattr(web, '_SESSIONS', {})
    detail = client.get('/api/sessions/' + sid, headers=HEADERS).json()
    assert detail['messages'][-1]['images'] == [image_id]


def test_quota_error_is_reported_without_images(monkeypatch):
    image_api(monkeypatch, errors.ClientError(429, {'error': {'message': 'quota', 'status': 'RESOURCE_EXHAUSTED'}}))
    agent, requests = agent_requesting()
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    sid = create(client)
    result = chat(client, sid)
    assert 'images' not in result['messages'][-1]
    assert '利用上限' in str(requests[1]['contents'][-2].parts[0].function_response.response)


def test_missing_image_output_is_an_error(monkeypatch):
    image_api(monkeypatch, text('no image'))
    stored = []
    with image_generation.generation_context(lambda *args: stored.append(args) or {}):
        result = image_generation.generate_image('猫')
    assert result['error_code'] == 'output_missing' and stored == []


def test_run_limit(monkeypatch):
    image_api(monkeypatch, gemini_image(picture()))
    stored = []
    with image_generation.generation_context(lambda body, mime, name: stored.append(name) or {'id': str(len(stored)), 'width': 1, 'height': 1}):
        results = [image_generation.generate_image('猫') for _ in range(image_generation.MAX_IMAGES_PER_RUN + 1)]
        assert image_generation.generated_ids() == ['1', '2', '3', '4']
    assert [r['status'] for r in results] == ['success'] * 4 + ['error']
    assert image_generation.generated_ids() == []


def test_web_ui_auto_renders_generated_images_and_has_no_mock_path():
    html = Path(__file__).resolve().parents[1].joinpath('static', 'index.html').read_text(encoding='utf-8')
    assert 'renderGeneratedImages(el,message.images' in html
    assert 'mock_image_path' not in html


def test_metrics_count_image_api_calls(monkeypatch):
    image_api(monkeypatch, gemini_image(picture()))
    agent, _ = agent_requesting()
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    metrics = chat(client, create(client))['metrics']
    assert metrics['image_api_calls'] == 1 and metrics['api_calls'] == 3


def test_durable_replay_keeps_generated_image_ids(monkeypatch, durable):
    image_api(monkeypatch, gemini_image(picture()))
    agent, _ = agent_requesting()
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    sid = create(client)
    body = {'session_id': sid, 'prompt': '猫の画像', 'request_id': 'req-image-1'}
    first = client.post('/api/chat', headers=HEADERS, json=body).json()
    monkeypatch.setattr(web, '_RUNS', {})
    replay = client.post('/api/chat', headers=HEADERS, json=body).json()
    assert first['generated_images'] and replay['generated_images'] == first['generated_images']


def test_failed_task_update_deletes_unreferenced_blob(durable):
    deleted = []
    durable.delete_object = lambda **kw: deleted.append(kw['Key'])
    service = attachments.AttachmentService('task-x', tasks.repository())
    service.document = lambda: {'lease': {'id': 'lease-1'}, 'images': {}}

    def fail(_):
        raise tasks.TaskStorageError('save failed', 'storage_unavailable')
    service._mutate = fail
    with pytest.raises(tasks.TaskStorageError):
        service.store_generated(attachments.normalize(picture(), 'image/png'), 'g.jpg', 'lease-1')
    assert len(durable.puts) == 1 and len(deleted) == 1
