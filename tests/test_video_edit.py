import io
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as web
from karry_agent import video_edit as ve
from karry_agent import video_edit_api

TOKEN = 'synthetic-owner-' + 'token'
AUTH = {'Authorization': 'Bearer ' + TOKEN}


class Missing(Exception):
    def __init__(self, code='NoSuchKey'):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


class FakeS3:
    def __init__(self):
        self.objects, self.uploads = {}, {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {'Body': io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing('404')
        return {'ContentLength': len(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, **_):
        return {'Contents': [{'Key': k, 'Size': len(v)} for k, v in sorted(self.objects.items()) if k.startswith(Prefix)], 'IsTruncated': False}

    def create_multipart_upload(self, Bucket, Key, ContentType):
        upload_id = f'u{len(self.uploads)}'
        self.uploads[upload_id] = {'key': Key, 'parts': {}}
        return {'UploadId': upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.uploads[UploadId]['parts'][PartNumber] = Body
        return {'ETag': f'e{PartNumber}'}

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker, MaxParts):
        if UploadId not in self.uploads:
            raise Missing('NoSuchUpload')
        parts = self.uploads[UploadId]['parts']
        return {'Parts': [{'PartNumber': n, 'ETag': f'e{n}', 'Size': len(b)} for n, b in sorted(parts.items())], 'IsTruncated': False}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        parts = self.uploads.pop(UploadId)['parts']
        self.objects[Key] = b''.join(parts[p['PartNumber']] for p in MultipartUpload['Parts'])

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.uploads.pop(UploadId, None)

    def generate_presigned_url(self, method, Params, ExpiresIn):
        return f'https://r2.example/{Params["Key"]}?m={method}'


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(ve, 'PART_SIZE', 4)
    s3 = FakeS3()
    dispatched = []
    app = FastAPI()
    app.include_router(video_edit_api.build_router(lambda: None, lambda: ve.EditStore(s3, 'bucket', 'edits/'), lambda job_id: dispatched.append(job_id) or 'ok'))
    return TestClient(app), s3, dispatched


def upload(client, data=b'0123456789', target=30):
    job = client.post('/api/video/edits', json={'filename': 'talk.mov', 'size': len(data), 'content_type': 'video/quicktime', 'target_minutes': target}).json()
    assert job['parts_total'] == 3 and job['status'] == 'uploading'
    for n in range(1, 4):
        assert client.put(f'/api/video/edits/{job["id"]}/parts/{n}', content=data[(n - 1) * 4:n * 4]).status_code == 200
    return job


def segments(n=6):
    return [{'id': i, 'start': i * 10.0, 'end': i * 10.0 + 8, 'speaker': 'A', 'text': f'発言{i}', 'keep': True, 'telop': ''} for i in range(n)]


def test_multipart_upload_completes_and_dispatches(env):
    client, s3, dispatched = env
    job = upload(client)
    assert client.get(f'/api/video/edits/{job["id"]}/parts').json()['parts'] == [1, 2, 3]
    done = client.post(f'/api/video/edits/{job["id"]}/complete')
    assert done.json()['status'] == 'queued_analyze'
    assert s3.objects[f'edits/{job["id"]}/source.mov'] == b'0123456789'
    for _ in range(50):
        if dispatched:
            break
        time.sleep(0.01)
    assert dispatched == [job['id']]
    assert 'upload_id' not in json.loads(s3.objects[f'edits/{job["id"]}/job.json'])


def test_upload_rejects_wrong_part_size_and_incomplete(env):
    client, _, _ = env
    job = client.post('/api/video/edits', json={'filename': 'a.mp4', 'size': 10}).json()
    assert client.put(f'/api/video/edits/{job["id"]}/parts/1', content=b'012').status_code == 422
    assert client.put(f'/api/video/edits/{job["id"]}/parts/4', content=b'01').status_code == 422
    assert client.post(f'/api/video/edits/{job["id"]}/complete').json()['code'] == 'upload_incomplete'
    assert client.post('/api/video/edits', json={'filename': 'a.mp4', 'size': ve.MAX_SOURCE_BYTES + 1}).status_code == 413
    assert client.post('/api/video/edits', json={'filename': 'a.mp4', 'size': 10, 'target_minutes': 0}).status_code == 422
    assert client.post('/api/video/edits', json={'filename': 'a.png', 'size': 10, 'content_type': 'image/png'}).status_code == 415


def test_worker_analyze_review_render_done(env, monkeypatch):
    client, s3, _ = env
    job = upload(client)
    client.post(f'/api/video/edits/{job["id"]}/complete')
    claim = client.post('/api/video/edits/worker/claim', json={}).json()['job']
    assert claim['stage'] == 'analyze' and claim['source_url'].startswith('https://r2.example/')
    assert client.get('/api/video/edits/worker/pending').json() == {'pending': 0}
    assert client.post('/api/video/edits/worker/claim', json={}).json() == {'job': None}
    assert client.post(f'/api/video/edits/worker/{job["id"]}/probe', json={'duration': 60, 'width': 1920, 'height': 1080}).status_code == 200
    monkeypatch.setattr(ve, 'transcribe_chunk', lambda audio, mime, offset, seconds: [s | {'start': s['start'] + offset, 'end': s['end'] + offset} for s in segments(3)])
    for index, offset in ((0, 0), (1, 30)):
        response = client.post(f'/api/video/edits/worker/{job["id"]}/transcribe?index={index}&offset={offset}&seconds=30', content=b'mp3', headers={'Content-Type': 'audio/mpeg'})
        assert response.json() == {'index': index, 'count': 3}
    monkeypatch.setattr(ve, 'plan_cut', lambda segs, target: {'title': 'タイトル', 'keep': [0, 1, 4], 'telops': {0: '見出し'}})
    review = client.post(f'/api/video/edits/worker/{job["id"]}/plan').json()
    assert review['status'] == 'review' and review['segment_count'] == 6 and review['telop_count'] == 1
    detail = client.patch(f'/api/video/edits/{job["id"]}', json={'segments': [{'id': 1, 'keep': False}, {'id': 4, 'telop': '次の話題', 'text': '直した字幕'}], 'title': '完成版'}).json()
    assert [s['keep'] for s in detail['segments']] == [True, False, False, False, True, False]
    assert client.post(f'/api/video/edits/{job["id"]}/render').json()['status'] == 'queued_render'
    work = client.post('/api/video/edits/worker/claim', json={'job_id': job['id']}).json()['job']
    assert work['stage'] == 'render' and work['width'] == 1920 and 'm=put_object' in work['output_url']
    assert [c['text'] for c in work['cues']] == ['発言0', '見出し', '直した字幕', '次の話題']
    assert client.post(f'/api/video/edits/worker/{job["id"]}/rendered', json={'duration': 16}).json()['code'] == 'output_missing'
    s3.objects[f'edits/{job["id"]}/output.mp4'] = b'mp4'
    done = client.post(f'/api/video/edits/worker/{job["id"]}/rendered', json={'duration': 16}).json()
    assert done['status'] == 'done' and done['output_size'] == 3
    links = client.get(f'/api/video/edits/{job["id"]}/download').json()
    assert links['url'].endswith('output.mp4?m=get_object') and links['preview_url']
    assert client.delete(f'/api/video/edits/{job["id"]}').status_code == 200
    assert not [k for k in s3.objects if job['id'] in k]


def test_worker_failure_and_retry(env):
    client, _, _ = env
    job = upload(client)
    client.post(f'/api/video/edits/{job["id"]}/complete')
    client.post('/api/video/edits/worker/claim', json={})
    failed = client.post(f'/api/video/edits/worker/{job["id"]}/failed', json={'message': 'ffmpeg failed: boom'}).json()
    assert failed['status'] == 'failed' and failed['failed_stage'] == 'analyze'
    assert client.post(f'/api/video/edits/{job["id"]}/retry').json()['status'] == 'queued_analyze'
    assert client.post(f'/api/video/edits/worker/{job["id"]}/plan').json()['code'] == 'not_leased'


def test_expired_lease_is_reclaimed_until_attempt_limit(env, monkeypatch):
    client, _, _ = env
    job = upload(client)
    client.post(f'/api/video/edits/{job["id"]}/complete')
    monkeypatch.setitem(ve.LEASE_SECONDS, 'analyze', -1)
    for _ in range(ve.MAX_ATTEMPTS):
        assert client.post('/api/video/edits/worker/claim', json={}).json()['job']['stage'] == 'analyze'
    assert client.post('/api/video/edits/worker/claim', json={}).json() == {'job': None}
    assert client.get(f'/api/video/edits/{job["id"]}').json()['status'] == 'failed'


def test_render_plan_merges_adjacent_and_shifts_cues():
    segs = segments(6)
    for s in segs:
        s['keep'] = s['id'] in {0, 1, 3}
    segs[3]['telop'] = '話題'
    plan = ve.render_plan({'segments': segs, 'duration': 60, 'burn_subtitles': True}, gap=2.5)
    assert plan['ranges'] == [{'start': 0.0, 'end': 18.2, 'out_start': 0.0}, {'start': 29.8, 'end': 38.2, 'out_start': 18.2}]
    assert plan['duration'] == 26.6
    assert {'start': 18.4, 'end': 24.4, 'text': '話題', 'kind': 'telop'} in plan['cues']
    assert not [c for c in ve.render_plan({'segments': segs, 'burn_subtitles': False})['cues'] if c['kind'] == 'sub']


def test_merge_chunks_drops_overlap_and_plan_trims(monkeypatch):
    merged = ve.merge_chunks({'1': [{'start': 9.0, 'end': 12.0, 'text': 'b'}], '0': [{'start': 0.0, 'end': 10.0, 'text': 'a'}]})
    assert [(s['id'], s['start'], s['text']) for s in merged] == [(0, 0.0, 'a'), (1, 10.0, 'b')]
    monkeypatch.setattr(ve, '_generate_json', lambda contents, max_tokens=0: {'title': 't', 'keep': list(range(6)), 'telops': [{'id': 2, 'text': 'x'}]})
    plan = ve.plan_cut(segments(6), 20)
    assert sum(8 for _ in plan['keep']) <= 20 * 1.08 and 2 in plan['keep']


def test_normalize_transcript_bounds():
    raw = [{'start': 1, 'end': 0.5, 'text': ' こんにちは {x} '}, {'start': 'bad', 'end': 2, 'text': 'x'}, {'start': 999, 'end': 1000, 'text': 'late'}]
    assert ve.normalize_transcript(raw, 600, 30) == [{'start': 601.0, 'end': 601.5, 'speaker': None, 'text': 'こんにちは （x）'}]


def test_app_routes_require_owner_token_and_cap_parts(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', TOKEN)
    for name in ('KARRY_WEBSITE_S3_ENDPOINT', 'KARRY_WEBSITE_S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(web.app)
    job_id = 'a' * 32
    assert client.get('/api/video/edits').status_code == 401
    assert client.put(f'/api/video/edits/{job_id}/parts/1', content=b'0' * 10).status_code == 401
    assert client.post('/api/video/edits', json={'filename': 'a.mp4', 'size': 1}, headers=AUTH).json()['code'] == 'storage_configuration'
    big = b'0' * (8 * 1024 * 1024 + 70_000)
    assert client.put(f'/api/video/edits/{job_id}/parts/1', content=big, headers=AUTH).status_code == 413
    assert client.patch(f'/api/video/edits/{job_id}', content=b'0' * 70_000, headers=AUTH).status_code == 413
