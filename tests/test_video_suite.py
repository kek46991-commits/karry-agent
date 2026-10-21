import base64
import io
import re
import subprocess
import uuid
import wave
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as web
from karry_agent import lipsync
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


def test_default_provider_is_free_local_and_paid_apis_need_explicit_opt_in(monkeypatch):
    monkeypatch.delenv('KARRY_VIDEO_PROVIDER', raising=False)
    monkeypatch.setenv('KARRY_VIDEO_GENERATION_ENABLED', '1')
    monkeypatch.setenv('REPLICATE_API_TOKEN', 'r8_' + 'synthetic')
    assert vs.adapter_from_env().name == 'local'
    monkeypatch.setenv('KARRY_VIDEO_PROVIDER', 'replicate')
    monkeypatch.delenv('KARRY_VIDEO_GENERATION_ENABLED', raising=False)
    with pytest.raises(vs.VideoError) as exc:
        vs.adapter_from_env()
    assert exc.value.status_code == 503 and exc.value.code == 'generation_disabled'
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


def wav_bytes(seconds=1.0, rate=16000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\x00\x10' * int(rate * seconds))
    return out.getvalue()


def probe(mp4):
    path = '/tmp/karry-probe-' + uuid.uuid4().hex + '.mp4'
    with open(path, 'wb') as handle:
        handle.write(mp4)
    info = subprocess.run([vs.ffmpeg_exe(), '-hide_banner', '-i', path], capture_output=True, text=True).stderr
    h, m, sec = re.search(r'Duration: (\d+):(\d+):([\d.]+)', info).groups()
    size = re.search(r'Video: h264.*?, (\d+)x(\d+)', info).groups()
    return int(h) * 3600 + int(m) * 60 + float(sec), tuple(map(int, size)), 'Audio: aac' in info


@pytest.mark.parametrize('aspect,size,seconds,with_image,with_audio', [
    ('9:16', (720, 1280), 3, True, True), ('16:9', (1280, 720), 2, False, False)])
def test_local_renderer_outputs_real_mp4(aspect, size, seconds, with_image, with_audio):
    mp4 = vs.LocalVideoRenderer().render('夕焼けの大阪', aspect, seconds, png((300, 200)) if with_image else None,
                                         wav_bytes() if with_audio else None)
    assert mp4[4:8] == b'ftyp'
    duration, actual, has_audio = probe(mp4)
    assert abs(duration - seconds) < 0.3 and actual == size and has_audio is with_audio


def test_local_renderer_rejects_bad_input_and_missing_ffmpeg(monkeypatch):
    with pytest.raises(vs.VideoError) as exc:
        vs.LocalVideoRenderer().render('p', '16:9', 121, None, None)
    assert exc.value.code == 'invalid_duration'
    with pytest.raises(vs.VideoError) as exc:
        vs.compose_frame('16:9', 'p', b'not an image')
    assert exc.value.code == 'invalid_image'
    monkeypatch.setenv('KARRY_FFMPEG', '/nonexistent/ffmpeg')
    with pytest.raises(vs.VideoError) as exc:
        vs.LocalVideoRenderer()
    assert exc.value.status_code == 503


class WavNarrator(FakeNarrator):
    name = 'fake'

    def synthesize(self, text, voice):
        super().synthesize(text, voice)
        return wav_bytes(0.5)


@pytest.fixture
def local_env(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', TOKEN)
    vs._MEMORY_DOCS.clear()
    vs._MEMORY_BLOBS.clear()
    narrator, queued = WavNarrator(), []
    test_app = web.FastAPI()
    test_app.include_router(web.video_suite_api.build_router(
        web.require_tool_auth, lambda sid: None, vs.LocalVideoRenderer, lambda: narrator, queued.append))
    return TestClient(test_app), narrator, queued


def test_local_job_completes_free_with_narration_inside_mp4(local_env):
    client, narrator, queued = local_env
    job = create(client, duration_seconds='2', narration_text='まいど、ジミーや。', files={'image': ('me.png', png(), 'image/png')}).json()
    assert job['provider'] == 'local' and job['status'] == 'processing' and len(queued) == 1
    assert [(s['clip_seconds'], s['play_seconds']) for s in job['segments']] == [(2, 2)]
    queued.pop()()
    done = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert done['status'] == 'succeeded' and done['narration']['status'] == 'succeeded' and done['narration']['muxed'] is True
    assert done['narration']['engine'] == 'fake'
    clip = client.get(done['segments'][0]['url'], headers=AUTH)
    assert clip.status_code == 200 and clip.headers['content-type'] == 'video/mp4'
    duration, size, has_audio = probe(clip.content)
    assert abs(duration - 2) < 0.3 and size == (720, 1280) and has_audio
    assert narrator.calls == [('まいど、ジミーや。', 'jimmy')]
    config = client.get('/api/video/config', headers=AUTH).json()
    assert config['enabled'] is True and config['provider'] == 'local' and config['clip_lengths'] == [120]


def test_local_job_without_any_tts_is_not_a_silent_success(local_env):
    client, narrator, queued = local_env
    narrator.fail = True
    job = create(client, duration_seconds='1', narration_text='テスト').json()
    queued.pop()()
    done = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert done['status'] == 'partial'
    assert done['narration'] == {'text': 'テスト', 'voice': 'jimmy', 'status': 'placeholder', 'muxed': True, 'engine': None, 'error': 'tts_failed'}
    assert probe(client.get(done['segments'][0]['url'], headers=AUTH).content)[2] is True


def test_local_job_without_narration_text_is_a_plain_success(local_env):
    client, narrator, queued = local_env
    job = create(client, duration_seconds='1').json()
    queued.pop()()
    done = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert done['status'] == 'succeeded' and done['narration'] is None and narrator.calls == []


def mp3_bytes():
    return subprocess.run([vs.ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-f', 'wav', '-i', 'pipe:0', '-f', 'mp3', 'pipe:1'],
                          input=wav_bytes(1.0), capture_output=True, check=True).stdout


class FakeGTTS:
    calls = []

    def __init__(self, text, lang, timeout):
        FakeGTTS.calls.append((text, lang))

    def write_to_fp(self, fp):
        fp.write(mp3_bytes())


def test_free_narrator_converts_key_free_tts_to_wav():
    FakeGTTS.calls = []
    wav = vs.FreeNarrator(tts_factory=FakeGTTS).synthesize('まいど', 'jimmy')
    assert wav.startswith(b'RIFF') and FakeGTTS.calls == [('まいど', 'ja')]
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnframes() / w.getframerate() > 0.5


def test_free_narrator_errors_are_masked():
    class Down(FakeGTTS):
        def write_to_fp(self, fp):
            raise OSError('connection refused to translate.google.com token=' + 'abc')
    with pytest.raises(vs.VideoError) as exc:
        vs.FreeNarrator(tts_factory=Down).synthesize('x', 'jimmy')
    assert exc.value.code == 'tts_failed' and 'token' not in str(exc.value)
    assert vs.FreeNarrator(tts_factory=None, ffmpeg='').available is False


def test_narrator_chain_prefers_gemini_then_falls_back_to_free():
    class Named(FakeNarrator):
        def __init__(self, name, fail=False, available=True):
            super().__init__(fail)
            self.name, self.available = name, available
    gemini, free = Named('gemini', available=False), Named('free')
    chain = vs.NarratorChain([gemini, free])
    assert chain.available and chain.synthesize('a', 'jimmy').startswith(b'RIFF') and chain.last_engine == 'free'
    gemini.available, gemini.fail = True, True
    chain.synthesize('b', 'jimmy')
    assert chain.last_engine == 'free' and gemini.calls == [('b', 'jimmy')]
    free.fail = True
    with pytest.raises(vs.VideoError):
        chain.synthesize('c', 'jimmy')
    none = vs.NarratorChain([Named('gemini', available=False)])
    with pytest.raises(vs.VideoError) as exc:
        none.synthesize('d', 'jimmy')
    assert exc.value.code == 'tts_configuration' and none.available is False


def test_placeholder_is_audible_wav():
    with wave.open(io.BytesIO(vs.placeholder_wav())) as w:
        frames = w.readframes(w.getnframes())
    assert max(abs(int.from_bytes(frames[i:i + 2], 'little', signed=True)) for i in range(0, 8000, 2)) > 1000


def test_interrupted_local_render_is_marked_failed(local_env, monkeypatch):
    client, _, queued = local_env
    job = create(client, duration_seconds='1').json()
    queued.clear()
    monkeypatch.setattr(vs.time, 'time', lambda: job['created_at'] + vs.LOCAL_RENDER_TIMEOUT + 301)
    done = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert done['status'] == 'failed'


def speech_wav(seconds=2.0, rate=16000):
    import math
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            loud = (i // (rate // 2)) % 2  # 0.5 s silence / 0.5 s tone
            frames += int(12000 * loud * math.sin(i * 0.2)).to_bytes(2, 'little', signed=True)
        w.writeframes(bytes(frames))
    return out.getvalue()


def test_lipsync_levels_follow_loudness_and_concat_runs():
    levels = lipsync.loudness_levels(speech_wav(), 15, 30)
    assert len(levels) == 30 and max(levels[:6]) == 0 and max(levels[9:14]) == lipsync.LEVELS - 1
    script = lipsync.concat_script(['/a', '/b'], [0, 0, 1, 1, 1, 0], 15)
    assert script.splitlines() == ['ffconcat version 1.0', "file '/a'", 'duration 0.1333', "file '/b'", 'duration 0.2000',
                                   "file '/a'", 'duration 0.0667', "file '/a'"]
    with pytest.raises(lipsync.LipSyncError) as exc:
        lipsync.loudness_levels(b'not a wav', 15, 3)
    assert exc.value.code == 'invalid_audio'


def test_lipsync_poses_open_the_mouth_and_blank_photo_has_no_face():
    frame = vs.compose_frame('16:9', 'p', png((640, 360)))
    with pytest.raises(lipsync.LipSyncError) as exc:
        lipsync.find_mouth(frame)
    assert exc.value.code == 'no_face'
    poses = lipsync.mouth_poses(frame, lipsync.Mouth(cx=700, cy=420, width=120, face_height=300))
    assert len(poses) == lipsync.LEVELS
    closed, opened = (Image.open(io.BytesIO(p)).convert('L') for p in (poses[0], poses[-1]))
    mouth_box = (650, 420, 750, 450)
    assert sum(opened.crop(mouth_box).getdata()) < sum(closed.crop(mouth_box).getdata()) * 0.8


def test_local_renderer_lipsync_outputs_mp4_and_needs_photo_and_voice(monkeypatch):
    monkeypatch.setattr(lipsync, 'find_mouth', lambda frame: lipsync.Mouth(cx=400, cy=500, width=120, face_height=300))
    mp4 = vs.LocalVideoRenderer().render('p', '9:16', 2, png((300, 200)), speech_wav(), lip_sync=True)
    duration, size, has_audio = probe(mp4)
    assert abs(duration - 2) < 0.3 and size == (720, 1280) and has_audio
    for image, audio, code in ((None, speech_wav(), 'no_photo'), (png(), None, 'no_narration')):
        with pytest.raises(lipsync.LipSyncError) as exc:
            vs.LocalVideoRenderer().render('p', '9:16', 2, image, audio, lip_sync=True)
        assert exc.value.code == code


def test_local_job_lipsync_succeeds_or_falls_back_with_reason(local_env, monkeypatch):
    client, narrator, queued = local_env
    assert client.get('/api/video/config', headers=AUTH).json()['lipsync_available'] is True
    job = create(client, duration_seconds='2', narration_text='まいど', lip_sync='true',
                 files={'image': ('me.png', png(), 'image/png')}).json()
    assert job['lipsync'] == {'status': 'processing'}
    queued.pop()()
    done = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert done['status'] == 'partial' and done['lipsync'] == {'status': 'fallback', 'error': 'no_face'}
    assert done['segments'][0]['status'] == 'succeeded' and done['narration']['muxed'] is True
    monkeypatch.setattr(lipsync, 'find_mouth', lambda frame: lipsync.Mouth(cx=400, cy=500, width=120, face_height=300))
    job = create(client, duration_seconds='2', narration_text='まいど', lip_sync='true',
                 files={'image': ('me.png', png(), 'image/png')}).json()
    queued.pop()()
    done = client.get(f"/api/video/jobs/{job['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()
    assert done['status'] == 'succeeded' and done['lipsync'] == {'status': 'succeeded'}
    assert probe(client.get(done['segments'][0]['url'], headers=AUTH).content)[2]
    plain = create(client, duration_seconds='2', narration_text='まいど').json()
    assert plain['lipsync'] is None
