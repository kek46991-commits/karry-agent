from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .middleware import VideoGuardMiddleware
from .router import create_router
from .service import VideoService

__version__ = "0.1.0"
__all__ = ["Settings", "VideoService", "VideoGuardMiddleware", "create_router", "create_app"]


def create_app(settings=None, *, provider_factory=None, renderer=None, auth_dependency=None):
    settings = settings or Settings()
    service = VideoService(settings, provider_factory=provider_factory, renderer=renderer)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="Karry Video Projects", version=__version__, lifespan=lifespan)
    app.state.video_service = service
    app.add_middleware(VideoGuardMiddleware, settings=settings, enforce_token=auth_dependency is None)
    app.include_router(create_router(service, auth_dependency=auth_dependency))
    return app
