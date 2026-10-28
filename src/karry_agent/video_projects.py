"""SQL動画プロジェクトを既存のKarry APIへ追加するブリッジ。"""
from __future__ import annotations

import logging
import os
import shutil
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from karry_video import Settings, VideoGuardMiddleware, VideoService, create_router
from karry_video.errors import VideoError

from .auth_token import configured_token, token_matches

logger = logging.getLogger(__name__)


def _target(path):
    return path == "/api/video/project-config" or any(path == root or path.startswith(root + "/") for root in ("/api/video/projects", "/api/video/project-jobs", "/api/video/scripts")) or path in {"/api/video/generate/preview", "/api/video/generate/render"}


class KarryVideoGuard(VideoGuardMiddleware):
    """所有者トークンを先に検証し、新しい動画ルートだけを容量制限します。"""

    def __init__(self, app, settings):
        super().__init__(app, settings, enforce_token=False)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not _target(scope.get("path", "")):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        scheme, _, supplied = headers.get(b"authorization", b"").strip().partition(b" ")
        token = configured_token()
        if not token or scheme.lower() != b"bearer" or not token_matches(supplied, token):
            code, status = ("unauthorized", 401) if token else ("api_token_not_configured", 503)
            message = "有効なKARRY_API_TOKENのBearer認証が必要です。" if token else "KARRY_API_TOKENを設定してください。"
            return await JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers={"Cache-Control": "no-store"})(scope, receive, send)
        return await super().__call__(scope, receive, send)


class _Component:
    def __init__(self, runtime, name):
        self.runtime, self.name = runtime, name

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            return getattr(getattr(self.runtime.require(), self.name), name)(*args, **kwargs)
        return invoke


class _ServiceProxy:
    def __init__(self, runtime):
        self.runtime = runtime
        self.repository = _Component(runtime, "repository")
        self.storage = _Component(runtime, "storage")
        self.settings = Settings(database_url="sqlite:///:memory:", api_token="")

    call = staticmethod(VideoService.call)

    async def generate(self, *args, **kwargs):
        return await self.runtime.require().generate(*args, **kwargs)

    async def resume(self, *args, **kwargs):
        return await self.runtime.require().resume(*args, **kwargs)


class VideoProjectRuntime:
    def __init__(self):
        self.service = None
        self.reason = "動画プロジェクト管理は起動していません。"
        self.settings_factory = Settings
        self.service_factory = VideoService

    def require(self):
        if self.service is None:
            raise VideoError("video_projects_unavailable", self.reason, 503)
        return self.service

    async def require_ready(self):
        self.require()

    async def start(self):
        if os.getenv("KARRY_VIDEO_PROJECTS_ENABLED", "1") != "1":
            self.reason = "動画プロジェクト管理は無効になっています。"
            return
        candidate = None
        try:
            candidate = self.service_factory(self.settings_factory())
            await candidate.start()
            self.service = candidate
            self.reason = None
        except Exception:
            self.reason = "動画DBまたは非公開ストレージを初期化できません。設定を確認してください。"
            logger.warning("video_projects_unavailable code=initialization_failed")
            if candidate:
                try:
                    await candidate.close()
                except Exception:
                    pass

    async def close(self):
        service, self.service = self.service, None
        if service:
            await service.close()

    def config(self):
        settings = self.service.settings if self.service else Settings(database_url="sqlite:///:memory:", api_token="")
        tools = bool(shutil.which(settings.ffmpeg_bin) and shutil.which(settings.ffprobe_bin))
        script_key = os.getenv("GEMINI_API_KEY", "").strip()
        return {"enabled": self.service is not None, "preview_available": self.service is not None and tools,
                "tts_available": bool(self.service and shutil.which(settings.espeak_bin)),
                "script_generation_available": bool(script_key and script_key not in {"your-gemini-api-key", "YOUR_GEMINI_API_KEY"}),
                "reason": self.reason if not self.service else (None if tools else "サーバーにFFmpegとffprobeが必要です。"),
                "max_seconds": 120, "job_prefix": "/api/video/project-jobs"}


def install(app, auth):
    runtime = VideoProjectRuntime()
    app.state.video_projects = runtime
    service = _ServiceProxy(runtime)
    app.add_middleware(KarryVideoGuard, settings=service.settings)
    router = create_router(service, auth_dependency=auth, job_prefix="/project-jobs")
    app.include_router(router, dependencies=[Depends(runtime.require_ready)])
    config_router = APIRouter(prefix="/api/video", dependencies=[Depends(auth)])

    @config_router.get("/project-config", tags=["video"])
    def project_config():
        return JSONResponse(runtime.config(), headers={"Cache-Control": "no-store"})

    app.include_router(config_router)
    return runtime


@asynccontextmanager
async def lifespan(app):
    runtime = getattr(app.state, "video_projects", None)
    if runtime:
        await runtime.start()
    try:
        yield
    finally:
        if runtime:
            await runtime.close()
