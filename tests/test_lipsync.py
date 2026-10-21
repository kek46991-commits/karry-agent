import io
import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_video_suite import AUTH, TOKEN, WavNarrator, create, png, probe

import app as web
from karry_agent import lipsync_remote as ls
from karry_agent import video_suite as vs

TOKEN_VALUE = 'synthetic-lipsync-' + 'credential'


def talking_mp4(seconds=1.0, size=256):
    return subprocess.run([vs.ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', f'testsrc=size={size}x{size}:rate=25',
                           '-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-f', 'mp4',
                           '-movflags', 'frag_keyframe+empty_moov', 'pipe:1'], capture_output=True, check=True).stdout


class Response:
    def __init__(self, status=200, body=b'', data=None, lines=()):
        self.status_code, self.body, self.data, self.lines = status, body, data, lines

    def json(self):
        if self.data is None:
            raise ValueError
        return self.data

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Session:
    trust_env = True

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append((method, url, headers or {}, kwargs))
        for (m, suffix), response in self.routes.items():
            if m == method and url.endswith(suffix):
                return response(kwargs) if callable(response) else response
        return Response(404)


def test_disabled_by_default_and_config_errors(monkeypatch):
    for name in ('KARRY_LIPSYNC_PROVIDER', 'KARRY_LIPSYNC_URL', 'KARRY_LIPSYNC_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(vs.VideoError) as exc:
        ls.lipsync_from_env()
    assert exc.value.code == 'lipsync_configuration'
    monkeypatch.setenv('KARRY_LIPSYNC_PROVIDER', 'http')
    with pytest.raises(vs.VideoError):
        ls.lipsync_from_env()
    for bad in ('http://gpu.example.com/run', 'https://user:pw@gpu.example.com', 'ftp://gpu.example.com', 'https://gpu.example.com/?k=v'):
        monkeypatch.setenv('KARRY_LIPSYNC_URL', bad)
        with pytest.raises(vs.VideoError) as exc:
            ls.lipsync_from_env()
        assert exc.value.code == 'lipsync_configuration' and 'pw' not in exc.value.message
    monkeypatch.setenv('KARRY_LIPSYNC_URL', 'https://gpu.example.com/lipsync')
    assert isinstance(ls.lipsync_from_env(), ls.HttpLipSyncAdapter)
    monkeypatch.setenv('KARRY_LIPSYNC_PROVIDER', 'gradio')
    monkeypatch.setenv('KARRY_LIPSYNC_GRADIO_ARGS', '{"x": 1}')
    with pytest.raises(vs.VideoError):
        ls.lipsync_from_env()
    monkeypatch.setenv('KARRY_LIPSYNC_GRADIO_ARGS', '["full", true]')
    adapter = ls.lipsync_from_env()
    assert isinstance(adapter, ls.GradioLipSyncAdapter) and adapter.extra_args == ['full', True]
    monkeypatch.setenv('KARRY_LIPSYNC_PROVIDER', 'replicate')
    with pytest.raises(vs.VideoError):
        ls.lipsync_from_env()


def test_http_adapter_contract_and_secret_safety():
    clip = talking_mp4()
    session = Session({('POST', '/lipsync'): Response(200, clip)})
    adapter = ls.HttpLipSyncAdapter('https://gpu.example.com/lipsync', TOKEN_VALUE, session=session)
    assert adapter.animate(png(), b'RIFFwav') == clip and session.trust_env is False
    method, url, headers, kwargs = session.calls[0]
    assert (method, url) == ('POST', 'https://gpu.example.com/lipsync') and headers['Authorization'] == 'Bearer ' + TOKEN_VALUE
    assert set(kwargs['files']) == {'image', 'audio'} and kwargs['allow_redirects'] is False
    for response, code in ((Response(200, b'<html>not a video</html>'), 'lipsync_response'), (Response(500, b'trace ' + TOKEN_VALUE.encode()), 'lipsync_failed'),
                           (Response(401), 'lipsync_rejected'), (Response(302), 'lipsync_response')):
        bad = ls.HttpLipSyncAdapter('https://gpu.example.com/lipsync', TOKEN_VALUE, session=Session({('POST', '/lipsync'): response}))
        with pytest.raises(vs.VideoError) as exc:
            bad.animate(png(), b'RIFFwav')
        assert exc.value.code == code and TOKEN_VALUE not in exc.value.message


def gradio_routes(clip, output_url='https://me-sadtalker.hf.space/gradio_api/file=/tmp/out.mp4', event='complete'):
    payload = [{'video': {'path': '/tmp/out.mp4', 'url': output_url}, 'subtitles': None}]
    return {('GET', '/config'): Response(data={'version': '5.25.2', 'api_prefix': '/gradio_api'}),
            ('POST', '/gradio_api/upload'): lambda kw: Response(data=['/tmp/gradio/' + kw['files']['files'][0]]),
            ('POST', '/gradio_api/call/test'): Response(data={'event_id': 'abc123'}),
            ('GET', '/gradio_api/call/test/abc123'): Response(lines=['event: generating', 'data: null', f'event: {event}', 'data: ' + json.dumps(payload)]),
            ('GET', '/file=/tmp/out.mp4'): Response(200, clip)}


def test_gradio_adapter_uploads_calls_and_downloads_same_origin():
    clip = talking_mp4()
    session = Session(gradio_routes(clip))
    adapter = ls.GradioLipSyncAdapter('https://me-sadtalker.hf.space', TOKEN_VALUE, session=session)
    assert adapter.animate(png(), b'RIFFwav') == clip
    call = next(c for c in session.calls if c[1].endswith('/call/test'))
    data = call[3]['json']['data']
    assert data[0]['path'] == '/tmp/gradio/face.png' and data[1]['path'] == '/tmp/gradio/narration.wav' and data[2:] == ls.DEFAULT_GRADIO_ARGS
    with pytest.raises(vs.VideoError) as exc:
        ls.GradioLipSyncAdapter('https://me-sadtalker.hf.space', TOKEN_VALUE,
                                session=Session(gradio_routes(clip, 'https://evil.example.com/file=/tmp/out.mp4'))).animate(png(), b'RIFFwav')
    assert exc.value.code == 'output_url'
    with pytest.raises(vs.VideoError) as exc:
        ls.GradioLipSyncAdapter('https://me-sadtalker.hf.space', session=Session(gradio_routes(clip, event='error'))).animate(png(), b'RIFFwav')
    assert exc.value.code == 'lipsync_failed'


def test_prepare_face_normalises_and_rejects_non_images():
    face = ls.prepare_face(png((3000, 1500)))
    with Image.open(io.BytesIO(face)) as image:
        assert image.format == 'PNG' and max(image.size) == ls.MAX_FACE_SIDE
    with pytest.raises(vs.VideoError) as exc:
        ls.prepare_face(b'not an image')
    assert exc.value.code == 'invalid_image'


class FakeLipSync:
    name = 'fake-sadtalker'

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def animate(self, face, wav):
        self.calls.append((face[:8], wav[:4]))
        if self.fail:
            raise vs.VideoError('down', 502, 'lipsync_unavailable')
        return talking_mp4(0.6)


@pytest.fixture
def talking_env(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', TOKEN)
    vs._MEMORY_DOCS.clear()
    vs._MEMORY_BLOBS.clear()
    narrator, queued, state = WavNarrator(), [], {'lipsync': FakeLipSync()}

    def factory():
        if state['lipsync'] is None:
            raise vs.VideoError('人物アニメーションの実行先が未設定です。', 503, 'lipsync_configuration')
        return state['lipsync']
    test_app = web.FastAPI()
    test_app.include_router(web.video_suite_api.build_router(
        web.require_tool_auth, lambda sid: None, vs.LocalVideoRenderer, lambda: narrator, queued.append, factory))
    return TestClient(test_app), narrator, queued, state


def run_job(client, queued, **fields):
    job = create(client, **fields)
    assert job.status_code == 202, job.text
    queued.pop()()
    return client.get(f"/api/video/jobs/{job.json()['id']}", params={'session_id': 'task-1'}, headers=AUTH).json()


def test_talking_mode_animates_photo_with_narration(talking_env):
    client, _narrator, queued, state = talking_env
    config = client.get('/api/video/config', headers=AUTH).json()
    assert config['modes'] == ['static', 'talking'] and config['lipsync'] == {'available': True, 'engine': 'fake-sadtalker', 'reason': None}
    done = run_job(client, queued, mode='talking', duration_seconds='2', narration_text='まいど、ジミーや。', files={'image': ('me.png', png(), 'image/png')})
    assert done['status'] == 'succeeded' and done['mode'] == 'talking'
    assert done['animation'] == {'status': 'succeeded', 'engine': 'fake-sadtalker'} and done['narration']['muxed'] is True
    assert state['lipsync'].calls == [(b'\x89PNG\r\n\x1a\n', b'RIFF')]
    duration, size, has_audio = probe(client.get(done['segments'][0]['url'], headers=AUTH).content)
    assert abs(duration - 2) < 0.3 and size == (720, 1280) and has_audio


def test_talking_mode_falls_back_to_static_and_reports_partial(talking_env):
    client, narrator, queued, state = talking_env
    state['lipsync'].fail = True
    done = run_job(client, queued, mode='talking', duration_seconds='1', aspect_ratio='16:9', narration_text='テスト', files={'image': ('me.png', png(), 'image/png')})
    assert done['status'] == 'partial' and done['animation'] == {'status': 'fallback', 'engine': 'fake-sadtalker', 'error': 'lipsync_unavailable'}
    _, size, has_audio = probe(client.get(done['segments'][0]['url'], headers=AUTH).content)
    assert size == (1280, 720) and has_audio
    narrator.fail, state['lipsync'] = True, FakeLipSync()
    done = run_job(client, queued, mode='talking', duration_seconds='1', narration_text='テスト', files={'image': ('me.png', png(), 'image/png')})
    assert done['status'] == 'partial' and done['animation']['error'] == 'narration_unavailable' and state['lipsync'].calls == []


def test_talking_mode_validation_and_unconfigured_engine(talking_env):
    client, _narrator, queued, state = talking_env
    photo = {'image': ('me.png', png(), 'image/png')}
    assert create(client, mode='talking', narration_text='やあ').json()['code'] == 'photo_required'
    assert create(client, mode='talking', files=photo).json()['code'] == 'narration_required'
    assert create(client, mode='dance', files=photo).json()['code'] == 'invalid_mode'
    state['lipsync'] = None
    response = create(client, mode='talking', narration_text='やあ', files={'image': ('me.png', png(), 'image/png')})
    assert response.status_code == 503 and response.json()['code'] == 'lipsync_configuration' and queued == []
    config = client.get('/api/video/config', headers=AUTH).json()['lipsync']
    assert config['available'] is False and config['engine'] is None and '未設定' in config['reason']
    static = run_job(client, queued, duration_seconds='1', files={'image': ('me.png', png(), 'image/png')})
    assert static['status'] == 'succeeded' and static['mode'] == 'static' and static['animation'] is None
