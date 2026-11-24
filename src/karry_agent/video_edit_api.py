"""Owner/worker API for long interview video editing (``/api/video/edits``)."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import video_edit as ve

logger = logging.getLogger(__name__)
JobId = Annotated[str, Path(pattern=r"^[a-f0-9]{32}$")]
AUDIO_TYPES = {"audio/mpeg", "audio/mp3", "audio/ogg", "audio/wav", "audio/aac", "audio/flac", "audio/mp4"}


class StartUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    size: int = Field(gt=0)
    content_type: str = Field(default="video/mp4", max_length=100)
    target_minutes: int = 30


class SegmentEdit(BaseModel):
    id: int
    keep: bool | None = None
    text: str | None = Field(default=None, max_length=400)
    telop: str | None = Field(default=None, max_length=100)


class EditPatch(BaseModel):
    segments: list[SegmentEdit] = Field(default_factory=list, max_length=ve.MAX_SEGMENTS)
    burn_subtitles: bool | None = None
    title: str | None = Field(default=None, max_length=200)


class Claim(BaseModel):
    job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class Probe(BaseModel):
    duration: float = Field(gt=0, le=6 * 3600)
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    has_audio: bool = True


class Rendered(BaseModel):
    duration: float = Field(gt=0)


class Failed(BaseModel):
    message: str = Field(default="", max_length=300)


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(exc: ve.EditError) -> JSONResponse:
    return _json({"detail": exc.message, "code": exc.code}, exc.status_code)


async def _body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise ve.EditError("1回の送信サイズが大きすぎます。", 413, "part_too_large")
    return bytes(body)


def build_router(auth: Callable[..., Any], store_factory: Callable[[], ve.EditStore] = ve.EditStore,
                 dispatch: Callable[[str], str] = ve.dispatch_worker) -> APIRouter:
    router = APIRouter(prefix="/api/video/edits", tags=["video-edit"], dependencies=[Depends(auth)])

    def kick(job: dict[str, Any]) -> None:
        def run() -> None:
            result = dispatch(job["id"])
            logger.info("video edit worker dispatch %s: %s", job["id"][:8], result)
        threading.Thread(target=run, name="video-edit-dispatch", daemon=True).start()

    def guarded(fn: Callable[[], Any]) -> JSONResponse:
        try:
            return fn()
        except ve.EditError as exc:
            return _error(exc)

    @router.get("")
    def list_jobs():
        return guarded(lambda: _json([ve.public_job(j) for j in store_factory().jobs()]))

    @router.post("")
    def start(payload: StartUpload):
        def run():
            job = store_factory().start_upload(payload.filename, payload.size, payload.content_type, payload.target_minutes)
            return _json(ve.public_job(job), 201)
        return guarded(run)

    @router.get("/worker/pending")
    def pending():
        return guarded(lambda: _json({"pending": ve.pending_count(store_factory())}))

    @router.post("/worker/claim")
    def claim(payload: Claim):
        def run():
            store = store_factory()
            job = store.claim(payload.job_id)
            if job is None:
                return _json({"job": None})
            stage = job.pop("_stage")
            work = {"job_id": job["id"], "stage": stage, "source_url": store.presign(job, job["source"])}
            if stage == "render":
                plan = ve.render_plan(job)
                work |= plan | {"output_url": store.presign(job, "output.mp4", "put_object", 3 * 3600, ContentType="video/mp4"),
                                "width": job.get("width"), "height": job.get("height")}
            return _json({"job": work})
        return guarded(run)

    @router.post("/worker/{job_id}/probe")
    def probe(job_id: JobId, payload: Probe):
        def run():
            store = store_factory()
            job = store.load(job_id)
            ve.require_lease(job, "analyzing")
            if not payload.has_audio:
                raise ve.EditError("音声が入っていない動画は文字起こしできません。", 422, "no_audio")
            job |= {"duration": round(payload.duration, 2), "width": payload.width, "height": payload.height, "chunks": {}}
            return _json(ve.public_job(store.save(job)))
        return guarded(run)

    @router.post("/worker/{job_id}/transcribe")
    async def transcribe(job_id: JobId, request: Request,
                         index: Annotated[int, Query(ge=0, le=100)], offset: Annotated[float, Query(ge=0)],
                         seconds: Annotated[float, Query(gt=0, le=1800)]):
        try:
            mime = request.headers.get("content-type", "audio/mpeg").split(";", 1)[0].strip()
            if mime not in AUDIO_TYPES:
                raise ve.EditError("音声形式が不正です。", 415, "unsupported_type")
            audio = await _body(request, ve.MAX_AUDIO_BYTES)
            if not audio:
                raise ve.EditError("音声が空です。", 422, "empty_audio")
            store = store_factory()
            ve.require_lease(await run_in_threadpool(store.load, job_id), "analyzing")
            segments = await run_in_threadpool(ve.transcribe_chunk, audio, mime, offset, seconds)

            def save():
                with store._lock:
                    job = store.load(job_id)
                    job.setdefault("chunks", {})[str(index)] = segments
                    store.save(job)
            await run_in_threadpool(save)
            return _json({"index": index, "count": len(segments)})
        except ve.EditError as exc:
            return _error(exc)

    @router.post("/worker/{job_id}/plan")
    def plan(job_id: JobId):
        def run():
            store = store_factory()
            job = store.load(job_id)
            ve.require_lease(job, "analyzing")
            job["segments"] = ve.merge_chunks(job.get("chunks") or {})
            result = ve.plan_cut(job["segments"], job["target_seconds"])
            ve.apply_plan(job, result)
            job |= {"status": "review", "chunks": {}, "attempts": 0, "lease_until": 0}
            return _json(ve.public_job(store.save(job)))
        return guarded(run)

    @router.post("/worker/{job_id}/rendered")
    def rendered(job_id: JobId, payload: Rendered):
        def run():
            store = store_factory()
            job = store.load(job_id)
            ve.require_lease(job, "rendering")
            size = store.exists(job, "output.mp4")
            if not size:
                raise ve.EditError("書き出した動画が見つかりません。", 409, "output_missing")
            job |= {"status": "done", "output_size": size, "output_duration": round(payload.duration, 1), "attempts": 0, "lease_until": 0}
            return _json(ve.public_job(store.save(job)))
        return guarded(run)

    @router.post("/worker/{job_id}/failed")
    def failed(job_id: JobId, payload: Failed):
        def run():
            store = store_factory()
            job = store.load(job_id)
            if job.get("status") not in {"analyzing", "rendering"}:
                return _json(ve.public_job(job))
            stage = "analyze" if job["status"] == "analyzing" else "render"
            message = ve._clean_text(payload.message, 200) or "処理に失敗しました。"
            job |= {"status": "failed", "failed_stage": stage, "error": message, "lease_until": 0}
            return _json(ve.public_job(store.save(job)))
        return guarded(run)

    @router.get("/{job_id}")
    def get_job(job_id: JobId):
        return guarded(lambda: _json(ve.public_job(store_factory().load(job_id), detail=True)))

    @router.get("/{job_id}/parts")
    def parts(job_id: JobId):
        def run():
            store = store_factory()
            job = store.load(job_id)
            done = [p["PartNumber"] for p in store.uploaded_parts(job)] if job["status"] == "uploading" else list(range(1, job["parts_total"] + 1))
            return _json({"parts": done, "parts_total": job["parts_total"], "part_size": ve.PART_SIZE})
        return guarded(run)

    @router.put("/{job_id}/parts/{number}")
    async def upload_part(job_id: JobId, number: Annotated[int, Path(ge=1, le=10000)], request: Request):
        try:
            body = await _body(request, ve.PART_SIZE)
            store = store_factory()
            job = await run_in_threadpool(store.load, job_id)
            await run_in_threadpool(store.upload_part, job, number, body)
            return _json({"part": number})
        except ve.EditError as exc:
            return _error(exc)

    @router.post("/{job_id}/complete")
    def complete(job_id: JobId):
        def run():
            store = store_factory()
            job = store.complete_upload(store.load(job_id))
            job["dispatch"] = "requested"
            kick(job)
            return _json(ve.public_job(job))
        return guarded(run)

    @router.patch("/{job_id}")
    def patch(job_id: JobId, payload: EditPatch):
        def run():
            store = store_factory()
            job = ve.apply_edits(store.load(job_id), payload.model_dump(exclude_none=True))
            return _json(ve.public_job(store.save(job), detail=True))
        return guarded(run)

    @router.post("/{job_id}/render")
    def render(job_id: JobId):
        def run():
            store = store_factory()
            job = store.load(job_id)
            if job.get("status") not in {"review", "done", "failed"} or not job.get("segments"):
                raise ve.EditError("編集プランができてから書き出してください。", 409, "not_ready")
            ve.render_plan(job)
            job |= {"status": "queued_render", "attempts": 0, "error": None, "failed_stage": None}
            kick(store.save(job))
            return _json(ve.public_job(job))
        return guarded(run)

    @router.post("/{job_id}/retry")
    def retry(job_id: JobId):
        def run():
            store = store_factory()
            job = store.load(job_id)
            if job.get("status") != "failed":
                raise ve.EditError("失敗したジョブだけ再試行できます。", 409, "not_failed")
            job |= {"status": "queued_render" if job.get("failed_stage") == "render" else "queued_analyze",
                    "attempts": 0, "error": None, "failed_stage": None}
            kick(store.save(job))
            return _json(ve.public_job(job))
        return guarded(run)

    @router.get("/{job_id}/download")
    def download(job_id: JobId):
        def run():
            store = store_factory()
            job = store.load(job_id)
            if job.get("status") != "done":
                raise ve.EditError("まだ完成していません。", 409, "not_ready")
            name = "".join(c for c in (job.get("title") or "interview") if c not in '\\/:*?"<>|')[:60] or "interview"
            return _json({"url": store.presign(job, "output.mp4", expires=6 * 3600,
                                               ResponseContentDisposition=f"attachment; filename*=UTF-8''{_quote(name)}.mp4"),
                          "preview_url": store.presign(job, "output.mp4", expires=6 * 3600)})
        return guarded(run)

    @router.delete("/{job_id}")
    def delete(job_id: JobId):
        def run():
            store = store_factory()
            store.delete(store.load(job_id))
            return _json({"deleted": job_id})
        return guarded(run)

    return router


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
