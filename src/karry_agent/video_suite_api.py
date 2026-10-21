"""FastAPI router for the Creative Video Suite (owner-authenticated)."""
from __future__ import annotations

import threading
import time
import uuid
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from . import video_suite as vs
from .image_attachments import MAX_UPLOAD_BYTES

SessionId = Annotated[str, Query(min_length=1, max_length=100)]


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(exc: vs.VideoError) -> JSONResponse:
    return _json({"detail": exc.message, "code": exc.code}, exc.status_code)


def build_router(auth: Callable[..., Any], check_session: Callable[[str], None],
                 adapter_factory: Callable[[], vs.VideoAdapter] | None = None,
                 narrator_factory: Callable[[], vs.GeminiNarrator] | None = None,
                 runner: Callable[[Callable[[], None]], None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/video", tags=["video"], dependencies=[Depends(auth)])
    adapters = adapter_factory or vs.adapter_from_env
    narrators = narrator_factory or vs.GeminiNarrator
    run_background = runner or (lambda fn: threading.Thread(target=fn, name="karry-video-render", daemon=True).start())

    def render_local(store: vs.VideoJobStore, job: dict[str, Any], renderer: Any, image: bytes | None, text: str, voice: str) -> None:
        wav = None
        if text:
            try:
                wav = narrators().synthesize(text, voice)
                store.put_blob(f"{job['id']}/narration.wav", wav, "audio/wav")
                job["narration"] |= {"status": "succeeded", "muxed": True}
            except vs.VideoError:
                wav = None  # silent video; the UI reads the text with the device voice
        segment = job["segments"][0]
        try:
            try:
                clip = renderer.render(job["prompt"], job["aspect_ratio"], job["duration_seconds"], image, wav)
            except vs.VideoError:
                if wav is None:
                    raise
                clip = renderer.render(job["prompt"], job["aspect_ratio"], job["duration_seconds"], image, None)
                job["narration"]["muxed"] = False
            store.put_blob(f"{job['id']}/0.mp4", clip, "video/mp4")
            segment["status"] = "succeeded"
        except vs.VideoError as exc:
            segment |= {"status": "failed", "error": exc.code}
        except Exception:
            segment |= {"status": "failed", "error": "render_failed"}
        job["status"] = vs.overall_status(job["segments"])
        job["updated_at"] = time.time()
        store.save(job)

    def refresh(store: vs.VideoJobStore, job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] != "processing":
            return job
        if job["provider"] == "local":
            # A restart (e.g. Render sleep) kills the render thread; do not leave the job spinning forever.
            if time.time() - job["updated_at"] > vs.LOCAL_RENDER_TIMEOUT + 300:
                job["segments"][0] |= {"status": "failed", "error": "render_interrupted"}
                job["status"], job["updated_at"] = "failed", time.time()
                store.save(job)
            return job
        adapter = adapters()
        for segment in job["segments"]:
            if segment["status"] != "processing":
                continue
            try:
                result = adapter.poll(segment["provider_id"])
                if result.status == "succeeded":
                    if not result.output_url:
                        raise vs.VideoError("生成結果に動画がありません。", 502, "output_missing")
                    store.put_blob(f"{job['id']}/{segment['index']}.mp4", adapter.download(result.output_url), "video/mp4")
                    segment["status"] = "succeeded"
                elif result.status == "failed":
                    segment |= {"status": "failed", "error": result.error}
            except vs.VideoError as exc:
                if exc.code in {"output_url", "output_capacity", "output_missing"}:
                    segment |= {"status": "failed", "error": exc.code}
        job["status"] = vs.overall_status(job["segments"])
        job["updated_at"] = time.time()
        store.save(job)
        return job

    @router.get("/config")
    def config():
        try:
            adapter = adapters()
            provider, lengths, enabled, reason = adapter.name, list(adapter.clip_lengths), True, None
        except vs.VideoError as exc:
            provider, lengths, enabled, reason = None, [], False, exc.message
        return _json({"enabled": enabled, "reason": reason, "provider": provider, "clip_lengths": lengths,
                      "aspect_ratios": list(vs.ASPECT_RATIOS), "min_seconds": vs.MIN_SECONDS, "max_seconds": vs.MAX_SECONDS,
                      "narration_available": narrators().available,
                      "voices": [{"id": k, "label": vs.VOICE_LABELS[k]} for k in vs.VOICES]})

    @router.post("/jobs")
    async def create(session_id: Annotated[str, Form(min_length=1, max_length=100)],
                     prompt: Annotated[str, Form(min_length=1, max_length=2000)],
                     aspect_ratio: Annotated[str, Form()] = "16:9",
                     duration_seconds: Annotated[int, Form()] = 5,
                     narration_text: Annotated[str, Form(max_length=1000)] = "",
                     voice: Annotated[str, Form()] = "jimmy",
                     request_id: Annotated[uuid.UUID | None, Form()] = None,
                     image: Annotated[UploadFile | None, File()] = None):
        try:
            if aspect_ratio not in vs.ASPECT_RATIOS:
                raise vs.VideoError("縦横比は 16:9 か 9:16 を選んでください。", 422, "invalid_aspect_ratio")
            if voice not in vs.VOICES:
                raise vs.VideoError("声の指定が不正です。", 422, "invalid_voice")
            body = await image.read(MAX_UPLOAD_BYTES + 1) if image is not None and image.filename else b""
            if len(body) > MAX_UPLOAD_BYTES:
                raise vs.VideoError("画像は4 MiBまでです。", 413, "image_too_large")
            rid = str(request_id or uuid.uuid4())

            def work():
                check_session(session_id)
                store = vs.VideoJobStore(session_id)
                existing = next((j for j in store.jobs().values() if j["request_id"] == rid), None)
                if existing:
                    return existing, 200
                adapter = adapters()
                if getattr(adapter, "local", False):
                    vs.plan_segments(duration_seconds, adapter.clip_lengths)
                    image_bytes = vs.compose_frame(aspect_ratio, prompt.strip(), body) if body else None
                    text = narration_text.strip()
                    job = {"id": uuid.uuid4().hex, "request_id": rid, "status": "processing", "prompt": prompt.strip(),
                           "aspect_ratio": aspect_ratio, "duration_seconds": duration_seconds, "provider": adapter.name,
                           "with_image": bool(body), "created_at": time.time(), "updated_at": time.time(),
                           "segments": [{"index": 0, "clip_seconds": duration_seconds, "play_seconds": duration_seconds, "status": "processing"}],
                           "narration": {"text": text, "voice": voice, "status": "failed", "muxed": False} if text else None}
                    store.save(job)
                    run_background(lambda: render_local(store, job, adapter, image_bytes, text, voice))
                    return job, 202
                segments = vs.plan_segments(duration_seconds, adapter.clip_lengths)
                data_uri = vs.image_data_uri(body) if body else None
                job = {"id": uuid.uuid4().hex, "request_id": rid, "status": "processing", "prompt": prompt.strip(),
                       "aspect_ratio": aspect_ratio, "duration_seconds": duration_seconds, "provider": adapter.name,
                       "with_image": bool(data_uri), "created_at": time.time(), "updated_at": time.time(),
                       "segments": [], "narration": None}
                for segment in segments:
                    part = job["prompt"] if len(segments) == 1 else f"{job['prompt']}（全{len(segments)}カット中 {segment['index'] + 1} カット目）"
                    try:
                        result = adapter.submit(part, aspect_ratio, segment["clip_seconds"], data_uri)
                        segment |= {"status": "processing", "provider_id": result.provider_id}
                    except vs.VideoError as exc:
                        if not job["segments"]:
                            raise
                        segment |= {"status": "failed", "error": exc.code}
                    job["segments"].append(segment)
                text = narration_text.strip()
                if text:
                    job["narration"] = {"text": text, "voice": voice, "status": "failed"}
                    try:
                        store.put_blob(f"{job['id']}/narration.wav", narrators().synthesize(text, voice), "audio/wav")
                        job["narration"]["status"] = "succeeded"
                    except vs.VideoError:
                        pass  # the UI falls back to on-device speech for the same text
                job["status"] = vs.overall_status(job["segments"])
                store.save(job)
                return job, 202

            job, status = await run_in_threadpool(work)
            return _json(vs.public_job(job, session_id), status)
        except vs.VideoError as exc:
            return _error(exc)

    @router.get("/jobs")
    def list_jobs(session_id: SessionId):
        check_session(session_id)
        jobs = sorted(vs.VideoJobStore(session_id).jobs().values(), key=lambda j: j["created_at"], reverse=True)
        return _json([vs.public_job(j, session_id) for j in jobs])

    def _job(store: vs.VideoJobStore, job_id: str) -> dict[str, Any]:
        job = store.jobs().get(job_id)
        if job is None:
            raise vs.VideoError("動画ジョブが見つかりません。", 404, "not_found")
        return job

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, session_id: SessionId):
        try:
            check_session(session_id)
            store = vs.VideoJobStore(session_id)
            return _json(vs.public_job(refresh(store, _job(store, job_id)), session_id))
        except vs.VideoError as exc:
            return _error(exc)

    @router.get("/jobs/{job_id}/clips/{index}")
    def clip(job_id: str, index: int, session_id: SessionId):
        try:
            check_session(session_id)
            store = vs.VideoJobStore(session_id)
            job = _job(store, job_id)
            if not any(s["index"] == index and s["status"] == "succeeded" for s in job["segments"]):
                raise vs.VideoError("この動画はまだできていません。", 404, "not_ready")
            return Response(store.get_blob(f"{job_id}/{index}.mp4"), media_type="video/mp4", headers={"Cache-Control": "private, no-store"})
        except vs.VideoError as exc:
            return _error(exc)

    @router.get("/jobs/{job_id}/narration")
    def narration(job_id: str, session_id: SessionId):
        try:
            check_session(session_id)
            store = vs.VideoJobStore(session_id)
            job = _job(store, job_id)
            if (job.get("narration") or {}).get("status") != "succeeded":
                raise vs.VideoError("ナレーション音声はありません。", 404, "not_ready")
            return Response(store.get_blob(f"{job_id}/narration.wav"), media_type="audio/wav", headers={"Cache-Control": "private, no-store"})
        except vs.VideoError as exc:
            return _error(exc)

    return router
