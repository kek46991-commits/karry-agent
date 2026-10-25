"""FastAPI router for the Creative Video Suite (owner-authenticated)."""
from __future__ import annotations

import asyncio
import base64
import os
import threading
import time
import uuid
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from . import lipsync, lipsync_remote
from . import video_suite as vs
from .image_attachments import MAX_UPLOAD_BYTES

SessionId = Annotated[str, Query(min_length=1, max_length=100)]


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(exc: vs.VideoError) -> JSONResponse:
    return _json({"detail": exc.message, "code": exc.code}, exc.status_code)


def build_router(auth: Callable[..., Any], check_session: Callable[[str], None],
                 adapter_factory: Callable[[], vs.VideoAdapter] | None = None,
                 narrator_factory: Callable[[], Any] | None = None,
                 runner: Callable[[Callable[[], None]], None] | None = None,
                 lipsync_factory: Callable[[], Any] | None = None,
                 lip_sync_factory: Callable[[], Any] | None = None,
                 lip_sync_normalizer: Callable[..., bytes] | None = None,
                 autopilot_text_generator: Callable[[str, int], dict[str, Any]] | None = None,
                 autopilot_image_generator: Callable[[bytes, int], dict[str, Any]] | None = None,
                 autopilot_renderer_factory: Callable[[], Any] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/video", tags=["video"], dependencies=[Depends(auth)])
    adapters = adapter_factory or vs.adapter_from_env
    narrators = narrator_factory or vs.narrator_from_env
    lipsyncs = lipsync_factory or lipsync_remote.lipsync_from_env
    run_background = runner or (lambda fn: threading.Thread(target=fn, name="karry-video-render", daemon=True).start())
    autopilot_renderer = autopilot_renderer_factory or vs.LocalVideoRenderer

    def lipsync_adapter():
        from .sadtalker import adapter_from_env
        if lip_sync_factory:
            return lip_sync_factory()
        import os
        if os.getenv("KARRY_SADTALKER_SPACE"):
            return adapter_from_env()
        class CompatibleAdapter:
            async def generate(self, image, audio):
                try:
                    engine = lipsyncs()
                except vs.VideoError:
                    from .sadtalker import SadTalkerError
                    raise SadTalkerError("sadtalker_configuration") from None
                return await asyncio.to_thread(engine.animate, lipsync_remote.prepare_face(image), audio)
        return CompatibleAdapter()

    def animate_legacy(job, renderer, face, image, wav):
        animation = job["animation"]
        if wav is None or (job["narration"] or {}).get("status") != "succeeded":
            animation |= {"status": "fallback", "error": "narration_unavailable"}
            return None
        try:
            talking = lipsyncs().animate(face, wav)
            clip = renderer.render_talking(talking, job["prompt"], job["aspect_ratio"], job["duration_seconds"], image, wav)
            animation["status"] = "succeeded"
            return clip
        except vs.VideoError as exc:
            animation |= {"status": "fallback", "error": exc.code}
        except Exception:
            animation |= {"status": "fallback", "error": "lipsync_failed"}
        return None

    def render_local(store: vs.VideoJobStore, job: dict[str, Any], renderer: Any, image: bytes | None, text: str, voice: str, face: bytes | None = None) -> None:
        wav = None
        if text:
            try:
                narrator = narrators()
                wav = narrator.synthesize(text, voice)
                store.put_blob(f"{job['id']}/narration.wav", wav, "audio/wav")
                engine = getattr(narrator, "last_engine", None) or getattr(narrator, "name", "tts")
                job["narration"] |= {"status": "succeeded", "muxed": True, "engine": engine}
            except vs.VideoError as exc:
                # Never report a silent narration as success: mux an audible placeholder and mark the job partial.
                wav = vs.placeholder_wav()
                job["narration"] |= {"status": "placeholder", "muxed": True, "error": exc.code}
            except Exception:
                wav = vs.placeholder_wav()
                job["narration"] |= {"status": "placeholder", "muxed": True, "error": "tts_failed"}
        segment = job["segments"][0]
        try:
            clip = animate_legacy(job, renderer, face, image, wav) if job.get("animation") and face else None
            if job.get("lip_sync", {}).get("requested"):
                job["lip_sync"] |= {"status": "fallback", "engine": "local", "fallback_reason": "audio_unavailable"}
                if image and wav and job["narration"]["status"] == "succeeded":
                    try:
                        from .sadtalker import SadTalkerError
                        animated = asyncio.run(lipsync_adapter().generate(image, wav))
                        clip = (lip_sync_normalizer or vs.normalize_lipsync_video)(
                            animated, wav, job["aspect_ratio"], job["duration_seconds"])
                        job["lip_sync"] = {"requested": True, "status": "succeeded", "engine": "sadtalker"}
                    except Exception as exc:
                        from .sadtalker import SadTalkerError
                        code = exc.code if isinstance(exc, SadTalkerError) else "sadtalker_output" if isinstance(exc, vs.VideoError) else "sadtalker_failed"
                        job["lip_sync"]["fallback_reason"] = code
            if clip is None and job.get("lipsync"):
                try:
                    if (job["narration"] or {}).get("status") != "succeeded":
                        raise lipsync.LipSyncError("no_narration")
                    clip = renderer.render(job["prompt"], job["aspect_ratio"], job["duration_seconds"], image, wav, lip_sync=True)
                    job["lipsync"] = {"status": "succeeded"}
                except lipsync.LipSyncError as exc:
                    job["lipsync"] = {"status": "fallback", "error": exc.code}
                except Exception:
                    job["lipsync"] = {"status": "fallback", "error": "lipsync_failed"}
            if clip is None:
                # An audio-mux failure must not be retried as a silent video.
                clip = renderer.render(job["prompt"], job["aspect_ratio"], job["duration_seconds"], image, wav)
            store.put_blob(f"{job['id']}/0.mp4", clip, "video/mp4")
            segment["status"] = "succeeded"
        except vs.VideoError as exc:
            segment |= {"status": "failed", "error": exc.code}
        except Exception:
            segment |= {"status": "failed", "error": "render_failed"}
        job["status"] = vs.overall_status(job["segments"])
        if job["status"] == "succeeded" and ((job["narration"] or {}).get("status") not in (None, "succeeded")
                                              or (job.get("lipsync") or {}).get("status") == "fallback"
                                              or (job.get("animation") or {}).get("status") == "fallback"):
            job["status"] = "partial"
        job["updated_at"] = time.time()
        store.save(job)

    def refresh(store: vs.VideoJobStore, job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] != "processing":
            return job
        if job["provider"] == "local":
            # A restart (e.g. Render sleep) kills the render thread; do not leave the job spinning forever.
            auto_scenes = int((job.get("autopilot") or {}).get("scene_count") or 0)
            budget = (vs.LOCAL_RENDER_TIMEOUT * auto_scenes + 600 if auto_scenes else
                      vs.LOCAL_RENDER_TIMEOUT + 300 + (lipsync_remote.MAX_TIMEOUT if job.get("mode") == "talking" else 0))
            if time.time() - job["updated_at"] > budget:
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
        try:
            animation_engine, animation_reason = lipsyncs().name, None
        except vs.VideoError as exc:
            animation_engine, animation_reason = None, exc.message
        from .sadtalker import configuration_from_env
        sadtalker_config = configuration_from_env()
        if not sadtalker_config["configured"] and animation_engine is not None:
            sadtalker_config["provider"] = "existing_external"
            sadtalker_config["timeout_seconds"] = lipsync_remote._timeout()
        sadtalker_config["configured"] = sadtalker_config["configured"] or animation_engine is not None
        sadtalker_config["local_fallback_available"] = bool(vs.ffmpeg_exe())
        return _json({"enabled": enabled, "modes": ["static", "mouth", "talking"],
                      "talking": {"available": animation_engine is not None and provider == "local", "engine": animation_engine,
                                  "reason": animation_reason if provider == "local" else "人物アニメーションは0円ローカル合成モードでだけ使えます。"}, "reason": reason, "provider": provider, "clip_lengths": lengths,
                      "aspect_ratios": list(vs.ASPECT_RATIOS), "min_seconds": vs.MIN_SECONDS, "max_seconds": vs.MAX_SECONDS,
                      "sadtalker": sadtalker_config,
                      "lipsync_available": bool(enabled and getattr(adapter, "local", False) and lipsync.available()),
                      "narration_available": narrators().available,
                      "voices": [{"id": k, "label": vs.VOICE_LABELS[k]} for k in vs.VOICES]})

    @router.post("/jobs")
    async def create(session_id: Annotated[str, Form(min_length=1, max_length=100)],
                     prompt: Annotated[str, Form(min_length=1, max_length=2000)],
                     aspect_ratio: Annotated[str, Form()] = "16:9",
                     duration_seconds: Annotated[int, Form()] = 5,
                     narration_text: Annotated[str, Form(max_length=1000)] = "",
                     voice: Annotated[str, Form()] = "jimmy",
                     lip_sync: Annotated[bool, Form()] = False,
                     sadtalker: Annotated[bool, Form()] = False,
                     mode: Annotated[str, Form()] = "static",
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
            if sadtalker and (not body or not narration_text.strip()):
                raise vs.VideoError("人物を喋らせるには人物画像と読み上げる文章が必要です。", 422, "lipsync_input_required")
            if sadtalker:
                mode = "talking"
            elif lip_sync and mode == "static":
                mode = "mouth"
            if mode not in {"static", "mouth", "talking"}:
                raise vs.VideoError("動画の種類は static / mouth / talking から選んでください。", 422, "invalid_mode")
            legacy_talking = mode == "talking" and not sadtalker
            lip_sync = mode == "mouth"
            if legacy_talking and not body:
                raise vs.VideoError("人物を喋らせるには、顔が写った写真を選んでください。", 422, "photo_required")
            if legacy_talking and not narration_text.strip():
                raise vs.VideoError("人物を喋らせるには、ナレーションの文章を入れてください。", 422, "narration_required")
            rid = str(request_id or uuid.uuid4())

            def work():
                check_session(session_id)
                store = vs.VideoJobStore(session_id)
                existing = next((j for j in store.jobs().values() if j["request_id"] == rid), None)
                if existing:
                    return existing, 200
                adapter = vs.LocalVideoRenderer() if sadtalker else adapters()
                if legacy_talking and not getattr(adapter, "local", False):
                    raise vs.VideoError("人物アニメーションは0円ローカル合成モードでだけ使えます。", 422, "mode_unavailable")
                if getattr(adapter, "local", False):
                    vs.plan_segments(duration_seconds, adapter.clip_lengths)
                    image_bytes = (base64.b64decode(vs.image_data_uri(body).split(",", 1)[1]) if sadtalker
                                   else vs.compose_frame(aspect_ratio, prompt.strip(), body)) if body else None
                    text = narration_text.strip()
                    job = {"id": uuid.uuid4().hex, "request_id": rid, "status": "processing", "prompt": prompt.strip(),
                           "aspect_ratio": aspect_ratio, "duration_seconds": duration_seconds, "provider": adapter.name,
                           "with_image": bool(body), "created_at": time.time(), "updated_at": time.time(),
                           "segments": [{"index": 0, "clip_seconds": duration_seconds, "play_seconds": duration_seconds, "status": "processing"}],
                           "narration": {"text": text, "voice": voice, "status": "failed", "muxed": False} if text else None}
                    face = lipsync_remote.prepare_face(body) if legacy_talking else None
                    engine = lipsyncs().name if legacy_talking else None
                    job["mode"] = mode
                    job["animation"] = {"status": "processing", "engine": engine} if legacy_talking else None
                    job["lipsync"] = {"status": "processing"} if lip_sync and not sadtalker else None
                    if sadtalker:
                        job["lip_sync"] = {"requested": True, "status": "processing", "engine": "sadtalker"}
                    store.save(job)
                    run_background(lambda: render_local(store, job, adapter, image_bytes, text, voice, face))
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

    def run_autopilot(store: vs.VideoJobStore, job: dict[str, Any], source_text: str, image_bytes: bytes | None) -> None:
        try:
            scene_count = 3
            try:
                if source_text:
                    if autopilot_text_generator:
                        draft = autopilot_text_generator(source_text, scene_count)
                    else:
                        from karry_video.script_generation import generate_script_breakdown
                        draft = generate_script_breakdown(source_text, scene_count)
                elif image_bytes:
                    if autopilot_image_generator:
                        draft = autopilot_image_generator(image_bytes, scene_count)
                    else:
                        from karry_video.script_generation import generate_script_from_image
                        draft = generate_script_from_image(image_bytes, scene_count)
                else:
                    raise vs.VideoError("文章か画像を1つ以上入力してください。", 422, "autopilot_input_required")
            except Exception as exc:
                from karry_video.errors import VideoError as ScriptVideoError
                if isinstance(exc, ScriptVideoError):
                    raise vs.VideoError(exc.message, exc.status_code, exc.code) from None
                raise

            scenes = draft.get("scenes") if isinstance(draft, dict) else None
            if not isinstance(scenes, list) or len(scenes) != scene_count:
                raise vs.VideoError("脚本生成のシーン構成を検証できませんでした。", 502, "script_generation_invalid_output")
            if any(not isinstance(scene, dict) or isinstance(scene.get("duration_seconds"), bool)
                   or not isinstance(scene.get("duration_seconds"), int) or not 1 <= scene["duration_seconds"] <= 60
                   for scene in scenes):
                raise vs.VideoError("シーンの長さを検証できませんでした。", 502, "script_generation_invalid_output")
            total_seconds = sum(scene["duration_seconds"] for scene in scenes)
            if not 15 <= total_seconds <= 30:
                raise vs.VideoError("生成された動画尺が15〜30秒の範囲外です。", 502, "script_generation_invalid_output")
            narration_text_all = draft.get("narration_script")
            if not isinstance(narration_text_all, str) or not narration_text_all.strip() or len(narration_text_all) > 1000:
                raise vs.VideoError("ナレーションを検証できませんでした。", 502, "script_generation_invalid_output")
            job["prompt"] = str(draft.get("visual_prompt") or "").strip()[:2000]
            job["duration_seconds"] = total_seconds
            job["narration"] = {"text": narration_text_all, "voice": "jimmy", "status": "processing", "muxed": True}
            job["autopilot"] = {"status": "processing", "title": str(draft.get("title") or "自動生成動画")[:200],
                                "scene_count": scene_count, "current_scene": 0}
            job["segments"] = [{"index": 0, "clip_seconds": total_seconds, "play_seconds": total_seconds, "status": "processing"}]
            store.save(job)

            narrator = narrators()
            renderer = autopilot_renderer()
            clips, audios, engines = [], [], []
            for position, scene in enumerate(scenes, start=1):
                narration = scene.get("narration")
                visual = scene.get("visual_prompt")
                seconds = scene.get("duration_seconds")
                if not isinstance(narration, str) or not narration.strip() or not isinstance(visual, str) or not visual.strip():
                    raise vs.VideoError("シーンのナレーションまたは映像案が空です。", 502, "script_generation_invalid_output")
                if not isinstance(seconds, int) or not 1 <= seconds <= 60:
                    raise vs.VideoError("シーンの長さを検証できませんでした。", 502, "script_generation_invalid_output")
                job["autopilot"]["current_scene"] = position
                job["updated_at"] = time.time()
                store.save(job)
                wav = narrator.synthesize(narration, "jimmy")
                audios.append(wav)
                engines.append(getattr(narrator, "last_engine", None) or getattr(narrator, "name", "tts"))
                clips.append(renderer.render(visual, "16:9", seconds, image_bytes, wav))

            final_audio = vs.concatenate_wav_clips(audios)
            final_video = vs.concatenate_mp4_clips(clips)
            store.put_blob(f"{job['id']}/narration.wav", final_audio, "audio/wav")
            store.put_blob(f"{job['id']}/0.mp4", final_video, "video/mp4")
            job["narration"] |= {"status": "succeeded", "muxed": True,
                                 "engine": engines[0] if len(set(engines)) == 1 else "mixed"}
            job["segments"][0]["status"] = "succeeded"
            job["autopilot"] |= {"status": "succeeded", "current_scene": scene_count}
            job["status"] = "succeeded"
        except vs.VideoError as exc:
            job["status"] = "failed"
            job["segments"][0] |= {"status": "failed", "error": exc.code}
            if job.get("narration"):
                job["narration"]["status"] = "failed"
                job["narration"]["muxed"] = False
            job.setdefault("autopilot", {}).update(status="failed", error=exc.code)
        except Exception:
            job["status"] = "failed"
            job["segments"][0] |= {"status": "failed", "error": "autopilot_failed"}
            if job.get("narration"):
                job["narration"]["status"] = "failed"
                job["narration"]["muxed"] = False
            job.setdefault("autopilot", {}).update(status="failed", error="autopilot_failed")
        finally:
            job["updated_at"] = time.time()
            store.save(job)

    @router.post("/autopilot")
    async def create_autopilot(session_id: Annotated[str, Form(min_length=1, max_length=100)],
                               novel_text: Annotated[str, Form(max_length=12_000)] = "",
                               request_id: Annotated[uuid.UUID | None, Form()] = None,
                               image: Annotated[UploadFile | None, File()] = None):
        try:
            source_text = novel_text.strip()
            body = await image.read(MAX_UPLOAD_BYTES + 1) if image is not None and image.filename else b""
            if image is not None:
                await image.close()
            if len(body) > MAX_UPLOAD_BYTES:
                raise vs.VideoError("画像は4 MiBまでです。", 413, "image_too_large")
            if not source_text and not body:
                raise vs.VideoError("小説テキストまたは画像を入力してください。", 422, "autopilot_input_required")
            if source_text and not autopilot_text_generator and not os.getenv("GEMINI_API_KEY", "").strip():
                raise vs.VideoError("自動脚本の生成にはGEMINI_API_KEYの設定が必要です。", 503, "script_ai_not_configured")
            if not source_text and body and not autopilot_image_generator and not os.getenv("GEMINI_API_KEY", "").strip():
                raise vs.VideoError("画像から脚本を作るにはGEMINI_API_KEYの設定が必要です。", 503, "script_ai_not_configured")
            if not narrators().available:
                raise vs.VideoError("音声合成エンジンを利用できません。", 503, "tts_configuration")
            if not vs.ffmpeg_exe():
                raise vs.VideoError("動画合成エンジン（ffmpeg）を利用できません。", 503, "video_configuration")
            if body:
                # Decode/normalize once before queuing so corrupt or disguised files fail synchronously.
                vs.compose_frame("16:9", "", body)
            rid = str(request_id or uuid.uuid4())

            def work():
                check_session(session_id)
                store = vs.VideoJobStore(session_id)
                existing = next((item for item in store.jobs().values() if item.get("request_id") == rid), None)
                if existing:
                    return existing, 200
                if any(item.get("mode") == "autopilot" and item.get("status") == "processing"
                       for item in store.jobs().values()):
                    raise vs.VideoError("このタスクでは別の自動動画を生成中です。完了後にもう一度お試しください。", 409,
                                        "autopilot_busy")
                now = time.time()
                job = {"id": uuid.uuid4().hex, "request_id": rid, "status": "processing", "prompt": "自動生成中",
                       "aspect_ratio": "16:9", "duration_seconds": 30, "provider": "local", "with_image": bool(body),
                       "created_at": now, "updated_at": now,
                       "segments": [{"index": 0, "clip_seconds": 30, "play_seconds": 30, "status": "processing"}],
                       "narration": None, "mode": "autopilot",
                       "autopilot": {"status": "processing", "title": "脚本を作成中", "scene_count": 3, "current_scene": 0}}
                store.save(job)
                run_background(lambda: run_autopilot(store, job, source_text, body or None))
                return job, 202

            job, status = await run_in_threadpool(work)
            return _json(vs.public_job(job, session_id), status)
        except vs.VideoError as exc:
            return _error(exc)
        except Exception:
            return _json({"detail": "自動動画生成を開始できませんでした。", "code": "autopilot_unavailable"}, 503)

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
