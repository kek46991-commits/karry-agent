"""Bounded, private image attachments. Upload/preview never invoke a model."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
import warnings
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from botocore.exceptions import BotoCoreError, ClientError
from . import task_persistence as tasks

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_PENDING = 4
MAX_ASSETS = 100
MAX_MEMORY_BYTES = 32 * 1024 * 1024
FORM_BODY_LIMIT = MAX_UPLOAD_BYTES + 64 * 1024
MIME_FORMATS = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP', 'image/gif': 'GIF'}
_ID = re.compile(r'[a-f0-9]{32}')
_DECODE_LIMIT = threading.BoundedSemaphore(1)
_MEMORY_LOCK = threading.RLock()
_MEMORY_DOCUMENTS: dict[str, dict[str, Any]] = {}
_MEMORY_BYTES: dict[tuple[str, str], bytes] = {}


class ImageAttachmentError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class LimitedBuffer(io.BytesIO):
    def write(self, body):
        if self.tell() + len(body) > MAX_IMAGE_BYTES:
            raise ImageAttachmentError('正規化後の画像は2 MiB以内にしてください。圧縮して再送してください。', 413)
        return super().write(body)


@dataclass(frozen=True)
class NormalizedImage:
    body: bytes
    mime_type: str
    width: int
    height: int
    warning: str = ''


def normalize(contents: bytes, mime_type: str) -> NormalizedImage:
    if mime_type not in MIME_FORMATS:
        raise ImageAttachmentError('サポートされていない画像形式です（JPG, PNG, WEBP, GIFのみ対応）')
    if not contents or len(contents) > MAX_UPLOAD_BYTES:
        raise ImageAttachmentError('画像は空でなく4 MiB以内にしてください。', 413)
    if not _DECODE_LIMIT.acquire(blocking=False):
        raise ImageAttachmentError('画像の処理中です。少し待ってから再送してください。', 429)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(contents)) as probe:
                if probe.format != MIME_FORMATS[mime_type]:
                    raise ImageAttachmentError('申告されたMIMEと画像の実形式が一致しません。')
                if probe.width * probe.height > MAX_PIXELS:
                    raise ImageAttachmentError('画像の画素数は16 MP以内にしてください。', 413)
                probe.verify()
            with Image.open(io.BytesIO(contents)) as original:
                original.seek(0)
                ImageOps.exif_transpose(original, in_place=True)
                oriented = original
                output_mime = 'image/png' if mime_type == 'image/gif' else mime_type
                converted = oriented.convert('RGB' if output_mime == 'image/jpeg' else 'RGBA')
                try:
                    converted.info.clear()
                    buffer = LimitedBuffer()
                    params = {'quality': 95, 'exif': b''} if output_mime == 'image/jpeg' else {'lossless': True, 'exif': b''} if output_mime == 'image/webp' else {}
                    converted.save(buffer, format=MIME_FORMATS[output_mime], **params)
                    body = buffer.getvalue()
                    return NormalizedImage(body, output_mime, converted.width, converted.height,
                        'GIFは先頭フレームの静止PNGとして扱います。' if mime_type == 'image/gif' else '')
                finally:
                    converted.close()
                    if oriented is not original:
                        oriented.close()
    except ImageAttachmentError:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageAttachmentError('画像が破損しているか、安全に読み込めません。') from None
    finally:
        _DECODE_LIMIT.release()


def valid_id(value: str) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def data_uri(body: bytes, mime: str) -> str:
    return 'data:' + mime + ';base64,' + base64.b64encode(body).decode('ascii')


def memory_document(sid: str) -> dict[str, Any]:
    with _MEMORY_LOCK:
        return _MEMORY_DOCUMENTS.setdefault(sid, {'images': {}, 'pending_images': []})


def clear_memory_session(sid: str) -> None:
    with _MEMORY_LOCK:
        _MEMORY_DOCUMENTS.pop(sid, None)
        for key in list(_MEMORY_BYTES):
            if key[0] == sid:
                _MEMORY_BYTES.pop(key)


def _active_lease(document: dict) -> None:
    import time
    lease = document.get('lease')
    if lease and lease['expires'] > time.time():
        raise tasks.TaskStorageError('同じタスクを処理中のため画像添付を変更できません。', 'session_busy')


class AttachmentService:
    def __init__(self, sid: str, repo: tasks.S3TaskRepository | None):
        self.sid = sid
        self.repo = repo

    def document(self) -> dict:
        return self.repo.load(self.sid) if self.repo else memory_document(self.sid)

    def _mutate(self, callback):
        if self.repo:
            return self.repo._cas(self.repo._key(self.sid), None, callback)
        with _MEMORY_LOCK:
            return callback(memory_document(self.sid))

    def _blob_key(self, image_id: str) -> str:
        if not valid_id(image_id):
            raise ImageAttachmentError('画像IDの形式が不正です。')
        return self.repo.prefix + 'images/' + hashlib.sha256(self.sid.encode()).hexdigest() + '/' + image_id + '.bin'

    def upload(self, image: NormalizedImage, filename: str) -> dict:
        import uuid
        document = self.document()
        _active_lease(document)
        uploading = len(document.get('image_uploads', {}))
        digest = hashlib.sha256(image.body).hexdigest()
        previous = next((meta for meta in document.get('image_uploads', {}).values() if meta['sha256'] == digest and meta['mime_type'] == image.mime_type), None)
        if previous is None and (len(document.get('images', {})) + uploading >= MAX_ASSETS or len(document.get('pending_images', [])) + uploading >= MAX_PENDING):
            raise ImageAttachmentError('タスクの画像数または次回添付の上限に達しました。', 409)
        image_id = previous['id'] if previous is not None else uuid.uuid4().hex
        label = tasks.credential_safe(PurePath((filename or 'image').replace('\\', '/')).name)[:120] or 'image'
        meta = {'id': image_id, 'filename': label, 'mime_type': image.mime_type, 'width': image.width, 'height': image.height,
                'size_bytes': len(image.body), 'sha256': digest, 'warning': image.warning}
        if previous is not None:
            meta = previous
        if self.repo:
            def reserve(value):
                self.repo._validate(value, self.sid)
                _active_lease(value)
                uploads = value.setdefault('image_uploads', {})
                if uploads.get(image_id) == meta:
                    return value
                if image_id in uploads or image_id in value.get('images', {}):
                    raise ImageAttachmentError('画像IDが競合しました。', 409)
                if len(value.get('images', {})) + len(uploads) >= MAX_ASSETS or len(value.get('pending_images', [])) + len(uploads) >= MAX_PENDING:
                    raise ImageAttachmentError('画像数の上限に達しました。', 409)
                uploads[image_id] = meta
                return value
            # Reserve before PUT. Ambiguous network failures retain a bounded,
            # reachable reservation and are reconciled on the next task read.
            self._mutate(reserve)
            try:
                response = self.repo.client.put_object(Bucket=self.repo.config.bucket, Key=self._blob_key(image_id), Body=image.body,
                    ContentType=image.mime_type, CacheControl='no-store', IfNoneMatch='*',
                    ContentMD5=base64.b64encode(hashlib.md5(image.body).digest()).decode(), Metadata={'sha256': meta['sha256']})
                if not response.get('ETag'):
                    raise tasks.TaskStorageError('画像の保存確認に失敗しました。', 'storage_unavailable')
            except ClientError as exc:
                if str(exc.response.get('Error', {}).get('Code', '')) not in {'PreconditionFailed', '412', 'ConditionalRequestConflict', '409'}:
                    raise tasks.TaskStorageError('画像の非公開保存に失敗しました。同じ画像を再選択して復旧できます。', 'storage_unavailable') from None
                self.get(image_id, document={'images': {image_id: meta}})
            except (BotoCoreError, OSError):
                raise tasks.TaskStorageError('画像の非公開保存に失敗しました。', 'storage_unavailable') from None
        else:
            with _MEMORY_LOCK:
                if sum(len(body) for body in _MEMORY_BYTES.values()) + len(image.body) > MAX_MEMORY_BYTES:
                    raise ImageAttachmentError('一時画像領域の容量上限に達しました。', 409)
                if (self.sid, image_id) in _MEMORY_BYTES:
                    raise ImageAttachmentError('画像IDが競合しました。再送してください。', 409)
                _MEMORY_BYTES[(self.sid, image_id)] = image.body
        def append(value):
            if self.repo:
                self.repo._validate(value, self.sid)
            _active_lease(value)
            images = value.setdefault('images', {})
            pending = value.setdefault('pending_images', [])
            if image_id in images:
                return value
            if self.repo and image_id not in value.get('image_uploads', {}):
                raise ImageAttachmentError('アップロードの予約が見つかりません。', 409)
            if len(images) >= MAX_ASSETS or len(pending) >= MAX_PENDING:
                raise ImageAttachmentError('画像数の上限に達しました。', 409)
            images[image_id] = meta
            pending.append(image_id)
            value.get('image_uploads', {}).pop(image_id, None)
            return value
        try:
            self._mutate(append)
        except Exception:
            if self.repo is None:
                with _MEMORY_LOCK:
                    if image_id not in _MEMORY_DOCUMENTS.get(self.sid, {}).get('images', {}):
                        _MEMORY_BYTES.pop((self.sid, image_id), None)
            raise
        return meta

    def reconcile(self, document: dict | None = None) -> dict:
        source = document if document is not None else self.document()
        if not self.repo:
            return source
        try:
            _active_lease(source)
        except tasks.TaskStorageError:
            return source
        for image_id, meta in list(source.get('image_uploads', {}).items()):
            try:
                self.get(image_id, document={'images': {image_id: meta}})
            except tasks.TaskStorageError:
                # No unverified image becomes pending, and the reservation still
                # counts towards the per-task object and pending limits.
                continue
            def finalize(value):
                self.repo._validate(value, self.sid)
                _active_lease(value)
                entry = value.get('image_uploads', {}).get(image_id)
                if entry is None:
                    return value
                if entry != meta:
                    raise tasks.TaskStorageError('アップロード予約の内容が変わりました。', 'storage_integrity')
                value.setdefault('images', {})[image_id] = meta
                value.setdefault('pending_images', []).append(image_id)
                value['image_uploads'].pop(image_id)
                self.repo._validate(value, self.sid)
                return value
            try:
                source = self._mutate(finalize)
            except tasks.TaskStorageError as exc:
                if exc.code not in {'session_busy', 'storage_conflict', 'storage_unavailable'}:
                    raise
                return source
        return source

    def get(self, image_id: str, document: dict | None = None) -> tuple[dict, bytes]:
        if not valid_id(image_id):
            raise ImageAttachmentError('画像IDの形式が不正です。')
        source = document if document is not None else self.document()
        meta = source.get('images', {}).get(image_id)
        if meta is None:
            raise ImageAttachmentError('このタスクの画像が見つかりません。', 404)
        if self.repo:
            response = None
            try:
                response = self.repo.client.get_object(Bucket=self.repo.config.bucket, Key=self._blob_key(image_id))
                if response.get('ContentLength') != meta['size_bytes'] or not 0 < meta['size_bytes'] <= MAX_IMAGE_BYTES or response.get('ContentType') != meta['mime_type']:
                    raise tasks.TaskStorageError('画像の保存内容が不正です。', 'storage_integrity')
                body = response['Body'].read(MAX_IMAGE_BYTES + 1)
                if len(body) != meta['size_bytes'] or hashlib.sha256(body).hexdigest() != meta['sha256'] or response.get('Metadata', {}).get('sha256') != meta['sha256']:
                    raise tasks.TaskStorageError('画像のハッシュ確認に失敗しました。', 'storage_integrity')
            except (BotoCoreError, ClientError, OSError):
                raise tasks.TaskStorageError('非公開画像の読取に失敗しました。', 'storage_unavailable') from None
            finally:
                if response and response.get('Body') is not None:
                    response['Body'].close()
        else:
            with _MEMORY_LOCK:
                body = _MEMORY_BYTES.get((self.sid, image_id))
            if body is None:
                raise ImageAttachmentError('一時画像が見つかりません。', 404)
            if len(body) != meta['size_bytes'] or hashlib.sha256(body).hexdigest() != meta['sha256']:
                raise ImageAttachmentError('一時画像の保存内容が一致しません。')
        return meta, body

    def detach(self, image_id: str) -> None:
        if not valid_id(image_id):
            raise ImageAttachmentError('画像IDの形式が不正です。')
        def remove(value):
            if self.repo:
                self.repo._validate(value, self.sid)
            _active_lease(value)
            if image_id not in value.get('images', {}):
                raise ImageAttachmentError('このタスクの画像が見つかりません。', 404)
            value['pending_images'] = [key for key in value.get('pending_images', []) if key != image_id]
            return value
        self._mutate(remove)

    def resolve(self, selected: list[str], document: dict | None = None) -> list[dict]:
        if len(selected) > MAX_PENDING or len(set(selected)) != len(selected):
            raise ImageAttachmentError('添付画像の選択が不正です。')
        source = document if document is not None else self.document()
        for image_id in selected:
            if image_id not in source.get('images', {}):
                raise ImageAttachmentError('このタスクの画像が見つかりません。', 404)
            if image_id not in source.get('pending_images', []):
                raise ImageAttachmentError('画像は取消済みまたは送信済みです。タスクを読み込み直してください。', 409)
        result = []
        for image_id in selected:
            meta, body = self.get(image_id, document=source)
            result.append({'data': body, 'mime_type': meta['mime_type']})
        return result
