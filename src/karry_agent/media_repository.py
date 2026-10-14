"""PostgreSQL media storage. Drizzle owns schema changes; no implicit DDL."""
from __future__ import annotations

import base64
import hashlib
import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .image_attachments import NormalizedImage, data_uri, MAX_IMAGE_BYTES

MAX_FILES = 100


class MediaError(ValueError):
    def __init__(self, message: str, status_code: int = 503, code: str = 'media_unavailable'):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class MediaRepository:
    @contextmanager
    def connection(self):
        url = os.getenv('DATABASE_URL', '')
        if not url.startswith(('postgres://', 'postgresql://')):
            raise MediaError('PostgreSQLのDATABASE_URLを秘密設定へ登録し、Migrationを適用してください。', code='database_configuration')
        try:
            with psycopg.connect(url, connect_timeout=5, row_factory=dict_row,
                options='-c statement_timeout=10000 -c lock_timeout=5000') as conn:
                yield conn
        except psycopg.Error:
            # Never include a DSN, provider SQL/error, prompt or encoded image.
            raise MediaError('メディアDBに接続できません。接続先・権限・Migrationを確認してください。', code='database_unavailable') from None

    def _lock(self, conn, sid):
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('media:' + sid,))

    def _capacity(self, conn, sid, table):
        # table is always an internal literal, never user input.
        count = conn.execute(f'SELECT count(*) AS n FROM {table} WHERE session_id=%s', (sid,)).fetchone()['n']
        if count >= MAX_FILES:
            raise MediaError('このタスクのメディア保存上限に達しました。', 409, 'media_capacity')

    def insert_upload(self, sid: str, image: NormalizedImage) -> dict:
        media_id = uuid.uuid5(uuid.NAMESPACE_URL, 'karry-upload:' + sid + ':' + hashlib.sha256(image.body).hexdigest())
        with self.connection() as conn:
            self._lock(conn, sid)
            row = conn.execute('SELECT * FROM media_files WHERE id=%s AND session_id=%s', (media_id, sid)).fetchone()
            if row:
                return row
            self._capacity(conn, sid, 'media_files')
            reserved = conn.execute("SELECT count(*) AS n FROM media_generation_jobs WHERE session_id=%s AND state IN ('submitting','processing','submission_unknown')", (sid,)).fetchone()['n']
            files = conn.execute('SELECT count(*) AS n FROM media_files WHERE session_id=%s', (sid,)).fetchone()['n']
            if files + reserved >= MAX_FILES:
                raise MediaError('画像生成用の保存枠を予約しているため、追加アップロードできません。', 409, 'media_capacity')
            return conn.execute('INSERT INTO media_files(id,session_id,type,file_url) VALUES(%s,%s,%s,%s) RETURNING *',
                (media_id, sid, 'image_input', data_uri(image.body, image.mime_type))).fetchone()

    def begin_job(self, sid: str, request_id: uuid.UUID, prompt: str) -> tuple[dict, bool]:
        with self.connection() as conn:
            self._lock(conn, sid)
            previous = conn.execute('SELECT * FROM media_generation_jobs WHERE session_id=%s AND request_id=%s', (sid, request_id)).fetchone()
            if previous:
                if previous['prompt'] != prompt:
                    raise MediaError('同じrequest_idで別の生成プロンプトを送信できません。', 409, 'request_conflict')
                return previous, False
            active = conn.execute("SELECT id FROM media_generation_jobs WHERE session_id=%s AND state IN ('submitting','processing','submission_unknown') LIMIT 1", (sid,)).fetchone()
            if active:
                raise MediaError('このタスクには処理中または受付確認が不明な生成があります。同じjob_idを確認してください。', 409, 'generation_busy')
            self._capacity(conn, sid, 'media_files')
            self._capacity(conn, sid, 'media_generation_jobs')
            row = conn.execute('INSERT INTO media_generation_jobs(session_id,request_id,prompt) VALUES(%s,%s,%s) RETURNING *',
                (sid, request_id, prompt)).fetchone()
            return row, True

    def set_submitted(self, job_id: uuid.UUID, prediction_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("UPDATE media_generation_jobs SET prediction_id=%s,state='processing',updated_at=now() WHERE id=%s AND state='submitting' RETURNING *", (prediction_id, job_id)).fetchone()
            if not row:
                raise MediaError('生成リクエストの状態が変更されました。', 409, 'job_conflict')
            return row

    def mark_unknown(self, job_id):
        with self.connection() as conn:
            conn.execute("UPDATE media_generation_jobs SET state='submission_unknown',error_code='submission_unknown',updated_at=now() WHERE id=%s AND state='submitting'", (job_id,))

    def reject_submission(self, job_id):
        with self.connection() as conn:
            conn.execute("UPDATE media_generation_jobs SET state='failed',error_code='submission_rejected',updated_at=now() WHERE id=%s AND state='submitting'", (job_id,))

    def fail(self, job_id, state='failed', error='prediction_failed'):
        if state not in {'failed', 'canceled'}:
            raise ValueError('invalid terminal state')
        with self.connection() as conn:
            conn.execute("UPDATE media_generation_jobs SET state=%s,error_code=%s,updated_at=now() WHERE id=%s AND state='processing'", (state, error, job_id))

    def complete(self, job: dict, image: NormalizedImage) -> dict:
        with self.connection() as conn:
            self._lock(conn, job['session_id'])
            current = conn.execute('SELECT * FROM media_generation_jobs WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
            if current['state'] == 'succeeded':
                return current
            if current['state'] != 'processing':
                raise MediaError('完了保存できない生成状態です。', 409, 'job_conflict')
            self._capacity(conn, job['session_id'], 'media_files')
            conn.execute('INSERT INTO media_files(id,session_id,type,file_url,prompt) VALUES(%s,%s,%s,%s,%s)',
                (job['id'], job['session_id'], 'image_generated', data_uri(image.body, image.mime_type), job['prompt']))
            return conn.execute("UPDATE media_generation_jobs SET state='succeeded',error_code=NULL,updated_at=now() WHERE id=%s RETURNING *", (job['id'],)).fetchone()

    def job(self, sid, job_id) -> dict:
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM media_generation_jobs WHERE id=%s AND session_id=%s', (job_id, sid)).fetchone()
            if not row:
                raise MediaError('このタスクの生成リクエストが見つかりません。', 404, 'not_found')
            return row

    def media(self, sid, media_id) -> dict:
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM media_files WHERE id=%s AND session_id=%s', (media_id, sid)).fetchone()
            if not row:
                raise MediaError('このタスクのメディアが見つかりません。', 404, 'not_found')
            return row

    def list_media(self, sid) -> list[dict]:
        with self.connection() as conn:
            # Never copy all base64 blobs into a listing response.
            return conn.execute('SELECT id,session_id,type,prompt,created_at FROM media_files WHERE session_id=%s ORDER BY created_at DESC,id DESC LIMIT %s', (sid, MAX_FILES)).fetchall()


def stored_image(row: dict) -> tuple[bytes, str]:
    value = row['file_url']
    if not isinstance(value, str) or len(value) > 4 * MAX_IMAGE_BYTES // 3 + 100:
        raise MediaError('保存済みメディアの形式・容量が不正です。', code='media_integrity')
    prefix, sep, encoded = value.partition(',')
    allowed = {'data:image/png;base64': 'image/png', 'data:image/jpeg;base64': 'image/jpeg', 'data:image/webp;base64': 'image/webp'}
    if not sep or prefix not in allowed:
        raise MediaError('保存済みメディアの形式が不正です。', code='media_integrity')
    try:
        body = base64.b64decode(encoded, validate=True)
        if not body or len(body) > MAX_IMAGE_BYTES:
            raise ValueError()
    except (ValueError, base64.binascii.Error):
        raise MediaError('保存済みメディアの内容が不正です。', code='media_integrity') from None
    return body, allowed[prefix]
