import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from .database import Database
from .errors import SubmissionUnknown, VideoError
from .preview import PreviewRenderer
from .providers import select_provider
from .repository import Repository, fingerprint
from .storage import Storage

logger = logging.getLogger(__name__)


class VideoService:
    """現在のプロセス内で有限ジョブを実行し、状態はDBに保存します。"""

    def __init__(self, settings, *, provider_factory=None, renderer=None):
        self.settings = settings
        self.database = Database(settings.database_url)
        self.repository = Repository(self.database)
        self.storage = Storage(settings)
        self.renderer = renderer or PreviewRenderer(settings, self.storage)
        self.provider_factory = provider_factory or select_provider
        self._tasks = {}
        self._semaphore = asyncio.Semaphore(settings.max_workers)
        self._lock = asyncio.Lock()
        self._closed = False
        self._started = False

    async def start(self):
        await self.call(self.database.initialize)
        self._started = True

    @staticmethod
    async def call(method, *args, **kwargs):
        return await asyncio.to_thread(method, *args, **kwargs)

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.call(self.database.close)

    def _track(self, job, owner, provider):
        task = asyncio.create_task(self._run(job, owner, provider), name=f"video-{job['id']}")
        self._tasks[job["id"]] = task
        def remove(done):
            if self._tasks.get(job["id"]) is done:
                self._tasks.pop(job["id"], None)
        task.add_done_callback(remove)

    async def generate(self, request, kind):
        if self._closed or not self._started:
            raise VideoError("service_unavailable", "動画サービスが起動していません。", 503)
        project_id, request_id = str(request.project_id), str(request.request_id)
        expected = fingerprint(kind, request.provider if kind == "render" else "local")
        async with self._lock:
            existing = await self.call(self.repository.find_request, project_id, request_id, expected)
            if existing:
                return existing
            if len(self._tasks) >= self.settings.queue_limit:
                raise VideoError("queue_full", "生成キューが混雑しています。後ほど実行してください。", 429)
            project = await self.call(self.repository.get_project, project_id)
            provider = None
            if kind == "render":
                provider = self.provider_factory(request.provider, self.settings)
                provider.validate(project)
            owner = str(uuid4())
            job, created = await self.call(self.repository.reserve_job, project_id, request_id, kind, provider.name if provider else None, expected, owner, self.settings.lease_seconds)
            if created:
                self._track(job, owner, provider)
            return job

    async def resume(self, job_id):
        async with self._lock:
            if self._closed or not self._started:
                raise VideoError("service_unavailable", "動画サービスが起動していません。", 503)
            if len(self._tasks) >= self.settings.queue_limit:
                raise VideoError("queue_full", "生成キューが混雑しています。", 429)
            job = await self.call(self.repository.get_job, str(job_id))
            if job["kind"] == "render" and job["phase"] == "submitting" and not job["provider_job_id"]:
                receipt = await self.call(self._read_receipt, job)
                if receipt:
                    await self.call(self.repository.reconcile_provider_id, job["id"], job["provider"], receipt)
                    job = await self.call(self.repository.get_job, job["id"])
            provider = self.provider_factory(job["provider"], self.settings) if job["kind"] == "render" else None
            owner = str(uuid4())
            claimed = await self.call(self.repository.claim_resume, job["id"], owner, self.settings.lease_seconds)
            self._track(claimed, owner, provider)
            return claimed

    def _write_receipt(self, job, provider_job_id):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", provider_job_id):
            raise SubmissionUnknown()
        directory = self.storage.path("receipts")
        directory.mkdir(mode=0o700, exist_ok=True)
        destination = self.storage.path(f"receipts/{job['id']}.json")
        fd, filename = tempfile.mkstemp(prefix="receipt-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"job_id": job["id"], "provider": job["provider"], "provider_job_id": provider_job_id}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(filename, destination)
        finally:
            Path(filename).unlink(missing_ok=True)

    def _read_receipt(self, job):
        path = self.storage.path(f"receipts/{job['id']}.json")
        if not path.is_file() or path.stat().st_size > 1024:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            accepted_id = value.get("provider_job_id", "")
            if value.get("job_id") == job["id"] and value.get("provider") == job["provider"] and re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", accepted_id):
                return accepted_id
        except (ValueError, TypeError, OSError):
            pass
        return None

    async def _set(self, job, owner, **values):
        if not await self.call(self.repository.update_job, job["id"], owner, **values):
            raise VideoError("lease_lost", "別のプロセスがジョブを取得しました。", 409)
        job.update(values)

    async def _heartbeat(self, job, owner):
        while True:
            await asyncio.sleep(min(self.settings.lease_seconds / 3, 10))
            if not await self.call(self.repository.heartbeat, job["id"], owner, self.settings.lease_seconds):
                return

    async def _render(self, project, output_key, video_source=None):
        # to_threadはcancelでは止まらないため、終了時も同期処理の完了を待ちます。
        task = asyncio.create_task(self.call(self.renderer.render, project, output_key, video_source=video_source))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _run(self, job, owner, provider):
        heartbeat = asyncio.create_task(self._heartbeat(job, owner))
        stage = None
        source = None
        output_key = None
        committed = False
        try:
            async with self._semaphore:
                if not await self.call(self.repository.heartbeat, job["id"], owner, self.settings.lease_seconds):
                    return
                project = job["snapshot"]
                output_key = self.storage.output_key(owner)
                if job["kind"] == "preview":
                    await self._set(job, owner, status="running", phase="assembling")
                    metadata = await self._render(project, output_key)
                else:
                    if not job["provider_job_id"]:
                        # 有料送信前にローカルTTS、入力画像、モデル制限を検証します。
                        await self._set(job, owner, status="running", phase="preparing")
                        await self.call(self.renderer.validate, project)
                        provider.validate(project)
                        stage = await self.call(self.storage.workspace, owner)
                        image = await self.call(self.storage.prepare_image, project["image_key"], project["aspect_ratio"], stage / "reference.png")
                        await self._set(job, owner, phase="submitting")
                        accepted_id = await provider.start(project, image)
                        # DB更新に失敗しても同じ有料リクエストを再送しないための受領記録。
                        await self.call(self._write_receipt, job, accepted_id)
                        await self._set(job, owner, provider_job_id=accepted_id, phase="polling")
                    else:
                        await self._set(job, owner, status="running", phase="polling")
                    deadline = time.monotonic() + self.settings.poll_timeout
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise VideoError("poll_timeout", "待機時間の上限に達しました。受領済みIDで状態確認を再開できます。", 504)
                        try:
                            state = await asyncio.wait_for(provider.poll(job["provider_job_id"]), timeout=remaining)
                        except asyncio.TimeoutError:
                            raise VideoError("poll_timeout", "待機時間の上限に達しました。受領済みIDで状態確認を再開できます。", 504) from None
                        if state.status == "succeeded":
                            if not state.output_url:
                                raise VideoError("output_missing", "生成結果を取得できません。IDを保持して再開できます。", 502)
                            source = self.storage.output_path(f"outputs/{owner}.source.mp4")
                            await self._set(job, owner, phase="downloading")
                            await provider.download(state.output_url, source)
                            break
                        if state.status in {"failed", "canceled"}:
                            raise VideoError("provider_failed", "外部プロバイダーが生成失敗またはキャンセルを報告しました。", 502)
                        if time.monotonic() >= deadline:
                            raise VideoError("poll_timeout", "待機時間の上限に達しました。受領済みIDで状態確認を再開できます。", 504)
                        await asyncio.sleep(self.settings.poll_interval)
                    await self._set(job, owner, phase="assembling")
                    metadata = await self._render(project, output_key, video_source=source)
                committed = await self.call(self.repository.finish_job, job["id"], owner, "succeeded", output_key=output_key, output_metadata=metadata)
        except asyncio.CancelledError:
            await self.call(self.repository.finish_job, job["id"], owner, "interrupted", error_code="worker_interrupted", release=False)
            raise
        except VideoError as error:
            unresolved = job["kind"] == "render" and (isinstance(error, SubmissionUnknown) or bool(job["provider_job_id"])) and error.code != "provider_failed"
            await self.call(self.repository.finish_job, job["id"], owner, "awaiting_resume" if unresolved else "failed", error_code=error.code, release=not unresolved)
            logger.warning("video_job_error job_id=%s code=%s", job["id"], error.code)
        except Exception:
            # 生のprovider応答、例外本文、署名URL、認証情報を記録しません。
            unresolved = job["kind"] == "render" and (job["phase"] == "submitting" or bool(job["provider_job_id"]))
            try:
                await self.call(self.repository.finish_job, job["id"], owner, "awaiting_resume" if unresolved else "failed", error_code="internal_error", release=not unresolved)
            except Exception:
                pass  # DB停止時には期限切れleaseと受領記録を用いて再開します。
            logger.warning("video_job_error job_id=%s code=internal_error", job["id"])
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if output_key and not committed:
                self.storage.path(output_key).unlink(missing_ok=True)
            if source:
                source.unlink(missing_ok=True)
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
