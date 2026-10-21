import base64
import io
import uuid
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as web
from karry_agent import video_suite as vs

TOKEN = 'synthetic-owner-' + 'token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


class FakeAdapter:
    name = 'fake'
    clip_lengths = (5, 10)

    def __init__(self):
        self.submitted, self.state = [], {}

    def submit(self, prompt, aspect_ratio, seconds, image_data_uri):
        pid = 'p' + str(len(self.submitted))
        self.submitted.append((prompt, aspect_ratio, seconds, image_data_uri))
        self.state[pid] = 'processing'
        return vs.Submission(pid, 'processing')

    def poll(self, pid):
        status = self.state[pid]
        return vs.Submission(pid, status, 'https://replicate.delivery/x.mp4' if status == 'succeeded' else None)

    def download(self, url):
        return b'\x00\x00\x00\x18ftypmp42fake'


class FakeNarrator:
    available = True

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def synthesize(self, text, voice):
        self.calls.append((text, voice))
        if self.fail:
            raise vs.VideoError('x', 502, 'tts_failed')
        return b'RIFF' + b'\x00' * 40


def png(size=(64, 48)):
    out = io.BytesIO()
    Image.new('RGB', size, (200, 30, 30)).save(out, 'PNG')
    return out.getvalue()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', TOKEN)
    vs._MEMORY_DOCS.clear()
    vs._MEMORY_BLOBS.clear()
    adapter, narrator = FakeAdapter(), FakeNarrator()
    test_app = web.FastAPI()
    test_app.include_router(web.video_suite_api.build_router(web.require_tool_auth, lambda sid: None, lambda: adapter, lambda: narrator))
    return TestClient(test_app), adapter, narrator


def create(client, **fields):
    data = {'session_id': 'task-1', 'prompt': '夕焼けの海辺を歩くジミー', 'aspect_ratio': '9:16', 'duration_seconds': '23', **fields}
    files = fields.pop('files', None)
    data.pop('files', None)
    return client.post('/api/video/jobs', data=data, files=files, headers=AUTH)


@pytest.mark.parametrize('duration,clips', [(1, (5, 10)), (5, (5, 10)), (23, (5, 10)), (120, (5, 10)), (1, tuple(range(2, 11))), (37, tuple(range(2, 11)))])
def test_segments_cover_duration_with_provider_lengths(duration, clips):
    segments = vs.plan_segments(duration, clips)
    assert sum(s['play_seconds'] for s in segments) == duration
    assert all(s['clip_seconds'] in clips and s['play_seconds'] <= s['clip_seconds'] for s in segments)
    assert [s['index'] for s in segments] == list(range(len(segments)))


@pytest.mark.parametrize('duration', [0, 121, -5])
def test_duration_out_of_range_rejected(duration):
    with pytest.raises(vs.VideoError) as exc:
        vs.plan_segments(duration, (5, 10))
    assert exc.value.code == 'invalid_duration'


def test_image_data_uri_stays_under_provider_limit():
    noisy = Image.effect_noise((3000, 3000), 120).convert('RGB')
    out = io.BytesIO()
    noisy.save(out, 'PNG')
    uri = vs.image_data_uri(out.getvalue())
    assert uri.startswith('data:image/jpeg;base64,')
    assert len(base64.b64decode(uri.split(',', 1)[1])) <= vs.MAX_DATA_URI_BYTES
    with pytest.raises(vs.VideoError):
        vs.image_data_uri(b'not an image')


def test_create_poll_and_serve_clips_with_narration(env):
    client, adapter, narrator = env
    response = create(client, narration_text='やあ、ジミーだ。', voice='jimmy', files={'image': ('me.png', png(), 'image/png')})
    assert response.status_code == 202
    job = response.json()
    assert job['status'] == 'processing' and job['aspect_ratio'] == '9:16' and job['with_image'] is True
    assert [s['clip_seconds'] for s in job['segments']] == [10, 10, 5]
    assert [s['play_seconds'] for s in job['segments']] == [10, 10, 3]
    assert all(call[1] == '9:16' and call[3].startswith('data:image/jpeg') for call in adapter.submitted)
    assert job['narration']['status'] == 'succeeded' and narrator.calls == [('やあ、ジミーだ。', 'jimmy')]
    assert 'provider_id' not in str(job) and 'replicate.delivery' not in str(job)

    adapter.state = {k: 'succeeded' for k in adapter.state}
    polled = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert polled['status'] == 'succeeded' and all('url' in s for s in polled['segments'])
    clip = client.get(polled['segments'][0]['url'], headers=AUTH)
    assert clip.status_code == 200 and clip.headers['content-type'] == 'video/mp4' and b'ftyp' in clip.content
    audio = client.get(polled['narration']['url'], headers=AUTH)
    assert audio.status_code == 200 and audio.content.startswith(b'RIFF')
    listed = client.get('/api/video/jobs', params={'session_id': 'task-1'}, headers=AUTH).json()
    assert [j['id'] for j in listed] == [job['id']]
    other = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-2'}, headers=AUTH)
    assert other.status_code == 404


def test_failed_segment_gives_partial_and_tts_failure_is_soft(env):
    client, adapter, narrator = env
    narrator.fail = True
    job = create(client, duration_seconds='15', narration_text='ナレーション').json()
    assert job['narration']['status'] == 'failed'
    adapter.state = {'p0': 'succeeded', 'p1': 'failed'}
    polled = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert polled['status'] == 'partial'
    assert client.get(f"/api/video/jobs/{job['id']}/clips/1", params={'session_id': 'task-1'}, headers=AUTH).status_code == 404


def test_request_id_is_idempotent(env):
    client, adapter, _ = env
    rid = str(uuid.uuid4())
    first = create(client, request_id=rid)
    second = create(client, request_id=rid)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()['id'] == second.json()['id'] and len(adapter.submitted) == 3


@pytest.mark.parametrize('fields,code', [({'aspect_ratio': '4:3'}, 'invalid_aspect_ratio'), ({'duration_seconds': '121'}, 'invalid_duration'),
                                         ({'duration_seconds': '0'}, 'invalid_duration'), ({'voice': 'robot'}, 'invalid_voice')])
def test_invalid_inputs_rejected_before_submission(env, fields, code):
    client, adapter, _ = env
    response = create(client, **fields)
    assert response.status_code == 422 and response.json()['code'] == code and adapter.submitted == []


def test_routes_require_owner_token(env):
    client, _, _ = env
    assert client.get('/api/video/config').status_code == 401
    assert client.get('/api/video/config', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    config = client.get('/api/video/config', headers=AUTH).json()
    assert config['aspect_ratios'] == ['16:9', '9:16'] and config['max_seconds'] == 120 and config['enabled'] is True


def test_disabled_provider_fails_closed(monkeypatch):
    monkeypatch.delenv('KARRY_VIDEO_GENERATION_ENABLED', raising=False)
    with pytest.raises(vs.VideoError) as exc:
        vs.adapter_from_env()
    assert exc.value.status_code == 503
    monkeypatch.setenv('KARRY_VIDEO_GENERATION_ENABLED', '1')
    monkeypatch.delenv('REPLICATE_API_TOKEN', raising=False)
    with pytest.raises(vs.VideoError) as exc:
        vs.adapter_from_env()
    assert exc.value.code == 'video_configuration'


def test_app_upload_guard_covers_video_jobs(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', TOKEN)
    client = TestClient(web.app)
    assert client.post('/api/video/jobs', data={'session_id': 's', 'prompt': 'p'}).status_code == 401
    big = {'image': ('a.png', b'0' * (web.image_attachments.FORM_BODY_LIMIT + 10), 'image/png')}
    assert client.post('/api/video/jobs', data={'session_id': 's', 'prompt': 'p'}, files=big, headers=AUTH).status_code == 413


def reply(status, body):
    response = Mock(status_code=status)
    response.json.return_value = body
    return response


def test_replicate_adapter_request_shape_and_secret_safety():
    token = 'r8_' + 'synthetic' * 3
    session = Mock()
    session.request.return_value = reply(201, {'id': 'abc123', 'status': 'starting'})
    adapter = vs.ReplicateVideoAdapter(token=token, session=session)
    result = adapter.submit('海', '16:9', 10, 'data:image/jpeg;base64,AAAA')
    method, url = session.request.call_args.args
    kwargs = session.request.call_args.kwargs
    assert (method, url) == ('POST', 'https://api.replicate.com/v1/models/kwaivgi/kling-v2.5-turbo-pro/predictions')
    assert kwargs['json'] == {'input': {'prompt': '海', 'duration': 10, 'aspect_ratio': '16:9', 'start_image': 'data:image/jpeg;base64,AAAA'}}
    assert kwargs['allow_redirects'] is False and kwargs['timeout'] == (5, 30) and result.status == 'processing'
    session.request.return_value = reply(200, {'id': 'abc123', 'status': 'succeeded', 'output': 'https://replicate.delivery/o.mp4'})
    assert adapter.poll('abc123').output_url == 'https://replicate.delivery/o.mp4'
    session.request.return_value = Mock(status_code=401, json=Mock(return_value={'detail': token}))
    with pytest.raises(vs.VideoError) as exc:
        adapter.submit('海', '16:9', 5, None)
    assert token not in exc.value.message
    session.request.return_value = Mock(status_code=402, json=Mock(return_value={'title': 'Insufficient credit', 'detail': token}))
    with pytest.raises(vs.VideoError) as exc:
        adapter.submit('海', '16:9', 5, None)
    assert exc.value.code == 'provider_credit' and 'クレジット' in exc.value.message and token not in exc.value.message


def test_runway_adapter_maps_ratio_and_model():
    session = Mock()
    task = str(uuid.uuid4())
    session.request.return_value = reply(200, {'id': task})
    adapter = vs.RunwayVideoAdapter(token='key_' + 'synthetic', session=session)
    adapter.submit('街', '9:16', 7, 'data:image/jpeg;base64,AAAA')
    assert session.request.call_args.args[1] == 'https://api.dev.runwayml.com/v1/image_to_video'
    body = session.request.call_args.kwargs['json']
    assert body['ratio'] == '720:1280' and body['duration'] == 7 and body['model'] == 'gen4_turbo' and body['promptImage']
    assert session.request.call_args.kwargs['headers']['X-Runway-Version'] == '2024-11-06'
    adapter.submit('街', '16:9', 3, None)
    assert session.request.call_args.args[1].endswith('/text_to_video')
    assert session.request.call_args.kwargs['json']['model'] == 'gen4.5' and session.request.call_args.kwargs['json']['ratio'] == '1280:720'
    session.request.return_value = reply(200, {'id': task, 'status': 'SUCCEEDED', 'output': ['https://d1.cloudfront.net/v.mp4']})
    assert adapter.poll(task).status == 'succeeded'
    with pytest.raises(vs.VideoError):
        adapter.poll('../etc')


@pytest.mark.parametrize('url', ['http://replicate.delivery/a', 'https://replicate.delivery.evil.invalid/a', 'https://u:p@replicate.delivery/a', 'https://localhost/a'])
def test_download_rejects_untrusted_urls(url):
    with pytest.raises(vs.VideoError) as exc:
        vs._download(url, ('replicate.delivery',))
    assert exc.value.code == 'output_url'


def test_gemini_narrator_uses_documented_interactions_api():
    wav = b'RIFF' + b'\x00' * 40
    session = Mock()
    session.post.return_value = reply(200, {'steps': [{'type': 'model_output', 'content': [{'type': 'audio', 'mime_type': 'audio/wav', 'data': base64.b64encode(wav).decode()}]}]})
    narrator = vs.GeminiNarrator(api_key='synthetic-gemini', session=session)
    assert narrator.synthesize('こんにちは', 'jimmy') == wav
    url = session.post.call_args.args[0]
    body = session.post.call_args.kwargs['json']
    assert url == 'https://generativelanguage.googleapis.com/v1beta/interactions'
    assert body['generation_config']['speech_config'][0]['voice'] == 'Charon' and body['response_format'] == {'type': 'audio'}
    assert session.post.call_args.kwargs['headers'] == {'x-goog-api-key': 'synthetic-gemini'}
    session.post.return_value = reply(500, {})
    with pytest.raises(vs.VideoError):
        narrator.synthesize('x', 'jimmy')
    assert vs.GeminiNarrator(api_key='').available is False
