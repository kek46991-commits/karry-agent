"""Private task snapshots in the same R2/S3 bucket; no Render filesystem writes."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Callable

from botocore.exceptions import BotoCoreError, ClientError
from . import website_storage as storage
from .self_healing import _SECRET_KEY

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_TASKS = 100
MAX_MESSAGES = 1000
LEASE_SECONDS = 1800


class TaskStorageError(storage.StorageError):
    pass


def mode() -> str:
    value = os.getenv("KARRY_TASK_STORAGE", "s3").strip().lower()
    if value not in {"s3", "memory"}:
        raise TaskStorageError("KARRY_TASK_STORAGEはs3またはmemoryを指定してください。", "storage_configuration")
    return value


def credential_safe(text: str) -> str:
    # Personal conversation data is private; credentials themselves must not be saved.
    for key, value in os.environ.items():
        value = value.strip()
        if key in {"PARTNER_NAME", "CORE_USER_NAME", "CORE_PRIVATE_CONTEXT"} or not value or not _SECRET_KEY.search(key):
            continue
        if len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
        else:
            text = re.sub(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", "[REDACTED]", text)
    return text


class S3TaskRepository:
    def __init__(self, config: storage.StorageConfig, client: Any = None):
        self.config = config
        self.client = client if client is not None else storage._client(config)
        self.prefix = os.getenv("KARRY_TASK_S3_PREFIX", "karry-tasks/").strip().rstrip("/") + "/"
        if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)+", self.prefix) or len(self.prefix) > 150 or self.prefix.startswith(config.prefix) or config.prefix.startswith(self.prefix):
            raise TaskStorageError("タスク保存prefixはHTMLと重複しない専用prefixを指定してください。", "storage_configuration")

    def _key(self, session_id: str) -> str:
        if not isinstance(session_id, str) or not 0 < len(session_id) <= 100:
            raise TaskStorageError("セッションIDの形式が不正です。", "invalid_session")
        if credential_safe(session_id) != session_id:
            raise TaskStorageError("資格情報をセッションIDとして指定できません。", "invalid_session")
        return self.prefix + "sessions/" + hashlib.sha256(session_id.encode()).hexdigest() + ".json"

    def _read(self, key: str) -> tuple[dict[str, Any] | None, str | None]:
        response = None
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            size = response.get("ContentLength", -1)
            if not isinstance(size, int) or not 0 < size <= MAX_DOCUMENT_BYTES or str(response.get("ContentType", "")).split(";", 1)[0] != "application/json":
                raise TaskStorageError("タスクJSONの形式・容量が不正です。", "storage_integrity")
            body = response["Body"].read(MAX_DOCUMENT_BYTES + 1)
            tag = response.get("ETag")
            if len(body) != size or hashlib.sha256(body).hexdigest() != response.get("Metadata", {}).get("sha256") or not isinstance(tag, str):
                raise TaskStorageError("タスクJSONの内容検証に失敗しました。", "storage_integrity")
            value = json.loads(body.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(value, dict):
                raise ValueError("object required")
            return value, tag
        except TaskStorageError:
            raise
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "NotFound", "404"}:
                return None, None
            raise TaskStorageError("タスクストレージの読み取りに失敗しました。", "storage_unavailable",
                                   diagnostic_code=storage.connection_diagnostic(exc)) from None
        except (BotoCoreError, OSError, UnicodeError, ValueError, KeyError) as exc:
            raise TaskStorageError("タスクJSONの読み取り・検証に失敗しました。", "storage_unavailable",
                                   diagnostic_code=storage.connection_diagnostic(exc)) from None
        finally:
            if response is not None and response.get("Body") is not None:
                response["Body"].close()

    def _write(self, key: str, value: dict[str, Any], tag: str | None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > MAX_DOCUMENT_BYTES:
            raise TaskStorageError("タスク履歴が保存容量上限に達しました。", "storage_capacity")
        try:
            response = self.client.put_object(Bucket=self.config.bucket, Key=key, Body=body,
                ContentType="application/json; charset=utf-8", CacheControl="no-store",
                Metadata={"sha256": hashlib.sha256(body).hexdigest()},
                ContentMD5=base64.b64encode(hashlib.md5(body).digest()).decode(),
                **({"IfMatch": tag} if tag else {"IfNoneMatch": "*"}))
            if not isinstance(response.get("ETag"), str):
                raise TaskStorageError("PUTの保存確認を取得できませんでした。", "storage_unavailable")
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}:
                raise TaskStorageError("タスクの保存が競合しました。", "storage_conflict") from None
            raise TaskStorageError("タスク履歴の永続保存に失敗しました。", "storage_unavailable",
                                   diagnostic_code=storage.connection_diagnostic(exc)) from None
        except (BotoCoreError, OSError) as exc:
            raise TaskStorageError("タスク履歴の永続保存に失敗しました。", "storage_unavailable",
                                   diagnostic_code=storage.connection_diagnostic(exc)) from None

    def _cas(self, key: str, default: dict[str, Any] | None, mutate: Callable) -> dict[str, Any]:
        for _ in range(3):
            current, tag = self._read(key)
            if current is None and default is None:
                raise TaskStorageError("Session not found", "not_found")
            current = current if current is not None else copy.deepcopy(default)
            updated = mutate(copy.deepcopy(current))
            if updated == current and tag is not None:
                return current
            try:
                self._write(key, updated, tag)
            except TaskStorageError as exc:
                if exc.code == "storage_conflict":
                    continue
                # A lost PUT acknowledgement must not cause another model execution.
                saved, _ = self._read(key)
                if saved == updated:
                    return saved
                raise
            saved = None
            for attempt in range(2):
                try:
                    saved, _ = self._read(key)
                    break
                except TaskStorageError as exc:
                    if exc.code != "storage_unavailable":
                        raise
            if saved is None:
                # A successful signed PUT with checksum and ETag is authoritative.
                # A failed optional read-back must not turn it into an unexecuted run.
                return updated
            if saved == updated:
                return saved
            # A concurrent metadata change is merged by the next bounded CAS pass.
        raise TaskStorageError("タスク更新の競合が解消しませんでした。再取得してください。", "storage_conflict")

    def _validate(self, value: dict[str, Any], sid: str) -> dict[str, Any]:
        messages = value.get("messages")
        if value.get("version") != 1 or value.get("session_id") != sid or not isinstance(value.get("title"), str) or len(value["title"]) > 120 or not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise TaskStorageError("保存済みタスクの構造が不正です。", "storage_integrity")
        for row in messages:
            if not isinstance(row, dict) or row.get("role") not in {"user", "model", "assistant"} or not isinstance(row.get("content"), str) or len(row["content"]) > 100_000:
                raise TaskStorageError("保存済み会話の構造が不正です。", "storage_integrity")
        images = value.get("images", {})
        pending = value.get("pending_images", [])
        uploads = value.get("image_uploads", {})
        if not isinstance(images, dict) or not isinstance(uploads, dict) or len(images) + len(uploads) > 100 or not isinstance(pending, list) or len(pending) + len(uploads) > 4 or any(not isinstance(key, str) for key in pending) or len(set(pending)) != len(pending) or set(images).intersection(uploads):
            raise TaskStorageError("画像添付の構造が不正です。", "storage_integrity")
        for key, meta in {**images, **uploads}.items():
            if not re.fullmatch(r"[a-f0-9]{32}", key) or not isinstance(meta, dict) or meta.get("id") != key or meta.get("mime_type") not in {"image/jpeg", "image/png", "image/webp"} or not isinstance(meta.get("filename"), str) or len(meta["filename"]) > 120 or not isinstance(meta.get("size_bytes"), int) or not 0 < meta["size_bytes"] <= 2 * 1024 * 1024 or not isinstance(meta.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", meta["sha256"]):
                raise TaskStorageError("保存画像の参照が不正です。", "storage_integrity")
            if type(meta.get("width")) is not int or type(meta.get("height")) is not int or meta["width"] <= 0 or meta["height"] <= 0 or meta["width"] * meta["height"] > 16_000_000 or not isinstance(meta.get("warning", ""), str) or len(meta.get("warning", "")) > 200:
                raise TaskStorageError("保存画像の寸法が不正です。", "storage_integrity")
        if any(not isinstance(key, str) or key not in images for key in pending):
            raise TaskStorageError("次回添付画像の参照が不正です。", "storage_integrity")
        for row in messages:
            ids = row.get("images", [])
            if not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(key, str) or key not in images for key in ids):
                raise TaskStorageError("会話画像の参照が不正です。", "storage_integrity")
        if not isinstance(value.get("latest_url"), str) or len(value["latest_url"]) > 2048 or value.get("last_run") is not None and not isinstance(value["last_run"], dict):
            raise TaskStorageError("保存済みメタ情報の構造が不正です。", "storage_integrity")
        lease = value.get("lease")
        if lease is not None and (not isinstance(lease, dict) or not isinstance(lease.get("id"), str) or not isinstance(lease.get("expires"), (int, float))):
            raise TaskStorageError("実行リースの構造が不正です。", "storage_integrity")
        outbox = value.get("publication_outbox", [])
        if not isinstance(outbox, list) or len(outbox) > 20 or any(
            not isinstance(row, dict) or not isinstance(row.get("filename"), str) or not storage.FILENAME_RE.fullmatch(row["filename"])
            or not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
            or not isinstance(row.get("public_url"), str) or len(row["public_url"]) > 2048
            or not row["public_url"].endswith("/static/instant/" + row["filename"])
            or not isinstance(row.get("created_at"), (int, float)) for row in outbox):
            raise TaskStorageError("公開復旧記録の構造が不正です。", "storage_integrity")
        return value

    def list(self) -> list[dict[str, str]]:
        value, _ = self._read(self.prefix + "index.json")
        if value is None:
            return []
        rows = value.get("tasks")
        if value.get("version") != 1 or not isinstance(rows, list) or len(rows) > MAX_TASKS or any(not isinstance(r, dict) or not isinstance(r.get("session_id"), str) or not 0 < len(r["session_id"]) <= 100 or not isinstance(r.get("title"), str) or len(r["title"]) > 120 for r in rows):
            raise TaskStorageError("タスク一覧の構造が不正です。", "storage_integrity")
        return [{"session_id": r["session_id"], "title": r["title"]} for r in rows]

    def create(self, sid: str, title: str) -> dict[str, Any]:
        if len(self.list()) >= MAX_TASKS:
            raise TaskStorageError("タスク数の上限に達しました。", "storage_capacity")
        key = self._key(sid)
        value = {"version": 1, "session_id": sid, "title": credential_safe(title), "messages": [], "latest_url": "", "last_run": None, "lease": None}
        if self._read(key)[0] is not None:
            raise TaskStorageError("セッションIDが競合しました。", "storage_conflict")
        try:
            self._write(key, value, None)
        except TaskStorageError as exc:
            if exc.code == "storage_conflict" or self._read(key)[0] != value:
                raise
        if self._read(key)[0] != value:
            raise TaskStorageError("タスク初期保存を検証できませんでした。", "storage_integrity")
        def index_update(index):
            rows = index.get("tasks")
            if index.get("version") != 1 or not isinstance(rows, list):
                raise TaskStorageError("タスク一覧の構造が不正です。", "storage_integrity")
            if any(r.get("session_id") == sid for r in rows):
                return index
            if len(rows) >= MAX_TASKS:
                raise TaskStorageError("タスク数の上限に達しました。", "storage_capacity")
            rows.append({"session_id": sid, "title": value["title"]})
            return index
        self._cas(self.prefix + "index.json", {"version": 1, "tasks": []}, index_update)
        return value

    def load(self, sid: str) -> dict[str, Any]:
        value, _ = self._read(self._key(sid))
        if value is None:
            raise TaskStorageError("Session not found", "not_found")
        return self._validate(value, sid)

    def begin(self, sid: str, lease_id: str) -> dict[str, Any]:
        def claim(value):
            self._validate(value, sid)
            if len(value["messages"]) + 2 > MAX_MESSAGES:
                raise TaskStorageError("履歴の上限に達しました。新しいタスクを作成してください。", "storage_capacity")
            lease = value.get("lease")
            if lease and lease["id"] != lease_id and lease["expires"] > time.time():
                raise TaskStorageError("同じタスクを別の実行が処理中です。", "session_busy")
            if not lease or lease["id"] != lease_id:
                value["lease"] = {"id": lease_id, "expires": time.time() + LEASE_SECONDS}
            return value
        return self._cas(self._key(sid), None, claim)

    def finish(self, sid: str, lease_id: str, messages: list[dict], run: dict) -> dict[str, Any]:
        if len(messages) > MAX_MESSAGES:
            raise TaskStorageError("履歴件数の上限に達しました。切り捨てず保存を停止します。", "storage_capacity")
        clean = [{"role": r["role"], "content": credential_safe(r["content"]),
                  **({"images": list(r["images"])} if r.get("images") else {})} for r in messages]
        # Pending confirmation payloads and authentication state are never persisted.
        summary = {key: copy.deepcopy(run[key]) for key in ("run_id", "status", "steps", "tool_calls", "metrics", "request_id_hash", "request_digest", "image_ids") if key in run}
        summary["trace"] = []
        summary["text"] = clean[-1]["content"] if clean and clean[-1]["role"] in {"model", "assistant"} else ""
        def commit(value):
            self._validate(value, sid)
            if (value.get("last_run") or {}).get("run_id") == run["run_id"] and value["messages"] == clean:
                return value
            lease = value.get("lease")
            if not lease or lease["id"] != lease_id or lease["expires"] <= time.time():
                raise TaskStorageError("タスク実行の保存権を確認できません。再取得してください。", "storage_conflict")
            value.update(messages=clean, last_run=summary, lease=None)
            if run.get("status") in {"completed", "needs_user_action"}:
                consumed = clean[-2].get("images", []) if len(clean) >= 2 else []
                value["pending_images"] = [key for key in value.get("pending_images", []) if key not in consumed]
            self._validate(value, sid)
            return value
        return self._cas(self._key(sid), None, commit)

    def release(self, sid: str, lease_id: str) -> None:
        def release(value):
            lease = value.get("lease")
            if lease and lease["id"] == lease_id:
                value["lease"] = None
            return value
        self._cas(self._key(sid), None, release)

    def update_url(self, sid: str, url: str) -> dict[str, Any]:
        def update(value):
            self._validate(value, sid)
            value["latest_url"] = url
            return value
        return self._cas(self._key(sid), None, update)

    def stage_publication(self, sid: str, plan: dict[str, Any]) -> None:
        intent = {key: plan[key] for key in ("filename", "sha256", "public_url")}
        if not storage.FILENAME_RE.fullmatch(intent["filename"]) or not re.fullmatch(r"[a-f0-9]{64}", intent["sha256"]):
            raise TaskStorageError("公開意図の形式が不正です。", "storage_integrity")
        intent["created_at"] = time.time()
        def stage(value):
            self._validate(value, sid)
            outbox = value.setdefault("publication_outbox", [])
            for row in outbox:
                if row["filename"] == intent["filename"]:
                    if row["sha256"] != intent["sha256"] or row["public_url"] != intent["public_url"]:
                        raise TaskStorageError("公開復旧記録と内容が一致しません。", "storage_integrity")
                    return value
            if len(outbox) >= 20:
                raise TaskStorageError("未確認の公開記録が上限に達しました。", "storage_capacity")
            outbox.append(intent)
            return value
        self._cas(self._key(sid), None, stage)

    def acknowledge_publication(self, sid: str, filename: str) -> dict[str, Any]:
        def acknowledge(value):
            self._validate(value, sid)
            outbox = value.get("publication_outbox", [])
            for intent in outbox:
                if intent["filename"] == filename and intent["created_at"] >= value.get("latest_publication_at", 0):
                    value["latest_url"] = intent["public_url"]
                    value["latest_publication_at"] = intent["created_at"]
            value["publication_outbox"] = [row for row in outbox if row["filename"] != filename]
            return value
        return self._cas(self._key(sid), None, acknowledge)

    def reconcile(self, sid: str, verify: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        document = self.load(sid)
        for intent in document.get("publication_outbox", []):
            if verify(intent):
                document = self.acknowledge_publication(sid, intent["filename"])
        return document


def repository() -> S3TaskRepository:
    try:
        return S3TaskRepository(storage.load_storage_config())
    except TaskStorageError:
        raise
    except storage.StorageError as exc:
        raise TaskStorageError(str(exc), exc.code) from None
    except (BotoCoreError, ClientError):
        raise TaskStorageError("タスクストレージの初期化に失敗しました。", "storage_unavailable") from None
