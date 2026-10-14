"""Replicate create once, poll explicitly, copy successful output into PostgreSQL."""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit

import requests

from .image_attachments import MAX_UPLOAD_BYTES, normalize
from .media_repository import MediaError, MediaRepository

API = 'https://api.replicate.com/v1'
MODEL = 'black-forest-labs/flux-schnell'
_PREDICTION = re.compile(r'[A-Za-z0-9_-]{1,80}')


class ReplicateClient:
    def __init__(self):
        token = os.getenv('REPLICATE_API_TOKEN', '')
        if not token or any(ch in token for ch in '\r\n'):
            raise MediaError('REPLICATE_API_TOKENをサーバーの秘密設定へ登録してください。', code='generation_configuration')
        self.api = requests.Session()
        self.api.trust_env = False
        self.api.headers.update(Authorization='Bearer ' + token, Accept='application/json')

    def _json(self, response):
        try:
            if response.status_code not in {200, 201} or len(response.content) > 128 * 1024:
                raise ValueError()
            result = response.json()
            if not isinstance(result, dict) or not _PREDICTION.fullmatch(str(result.get('id', ''))):
                raise ValueError()
            if result.get('status') not in {'starting', 'processing', 'succeeded', 'failed', 'canceled'}:
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise MediaError('画像生成APIの応答を確認できませんでした。生成を自動再送しません。', 502, 'provider_response') from None

    def create(self, prompt):
        try:
            # Official model endpoint: an owner/model name is not a version hash.
            response = self.api.post(API + '/models/' + MODEL + '/predictions',
                json={'input': {'prompt': prompt, 'num_outputs': 1, 'output_format': 'png'}},
                headers={'Prefer': 'wait=5', 'Cancel-After': '120s'}, timeout=(5, 20), allow_redirects=False)
            if response.status_code in {400, 401, 403, 404, 422, 429}:
                raise MediaError('生成受付が拒否されました。キー・入力・利用設定を確認してください。', 502, 'submission_rejected')
            return self._json(response)
        except requests.RequestException:
            raise MediaError('画像生成の受付確認が不明です。request_idを変えて自動再送しないでください。', 502, 'submission_unknown') from None

    def get(self, prediction_id):
        if not _PREDICTION.fullmatch(str(prediction_id)):
            raise MediaError('保存された生成IDが不正です。', code='job_integrity')
        try:
            result = self._json(self.api.get(API + '/predictions/' + prediction_id, timeout=(5, 15), allow_redirects=False))
            if result['id'] != prediction_id:
                raise MediaError('生成IDが一致しません。', 502, 'provider_response')
            return result
        except requests.RequestException:
            raise MediaError('生成状態を取得できません。新規生成せず同じjob_idを確認してください。', 502, 'provider_unavailable') from None

    def output(self, prediction):
        output = prediction.get('output')
        url = output[0] if isinstance(output, list) and len(output) == 1 else None
        if not isinstance(url, str):
            raise MediaError('生成結果に画像がありません。', 502, 'output_missing')
        parsed = urlsplit(url)
        try:
            allowed = parsed.scheme == 'https' and parsed.port in {None, 443} and parsed.username is None and parsed.password is None and not parsed.fragment
        except ValueError:
            allowed = False
        host = parsed.hostname or ''
        if not allowed or not (host == 'replicate.delivery' or host.endswith('.replicate.delivery')):
            raise MediaError('生成結果の保存先URLを検証できません。', 502, 'output_url')
        response = None
        try:
            # No API Authorization header is ever sent to the file origin.
            with requests.Session() as download:
                download.trust_env = False
                response = download.get(url, timeout=(5, 10), stream=True, allow_redirects=False,
                                        headers={'Accept-Encoding': 'identity'})
                if response.status_code != 200 or response.headers.get('Content-Encoding', 'identity') not in {'identity', ''}:
                    raise MediaError('生成画像を取得できません。再生成せず同じjob_idを確認してください。', 502, 'output_download')
                mime = response.headers.get('Content-Type', '').split(';')[0].strip()
                body = bytearray()
                deadline = time.monotonic() + 20
                while True:
                    if time.monotonic() >= deadline:
                        raise MediaError('生成画像の取得期限を超えました。同じjob_idを確認してください。', 502, 'output_download')
                    chunk = response.raw.read1(65536)
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_UPLOAD_BYTES:
                        raise MediaError('生成画像の容量上限を超えました。', 413, 'output_capacity')
                return normalize(bytes(body), mime)
        except requests.RequestException:
            raise MediaError('生成画像の取得に失敗しました。新規生成せず同じjob_idを確認してください。', 502, 'output_download') from None
        finally:
            if response is not None:
                response.close()


def refresh_job(repo: MediaRepository, job: dict, client: ReplicateClient, prediction: dict | None = None) -> dict:
    if job['state'] != 'processing':
        return job
    result = prediction if prediction is not None else client.get(job['prediction_id'])
    if result['id'] != job['prediction_id']:
        raise MediaError('生成IDが一致しません。', 502, 'provider_response')
    if result['status'] == 'succeeded':
        # Public/expiring provider URLs are not the durable source of truth.
        return repo.complete(job, client.output(result))
    if result['status'] in {'failed', 'canceled'}:
        repo.fail(job['id'], result['status'], 'prediction_' + result['status'])
        return repo.job(job['session_id'], job['id'])
    return job
