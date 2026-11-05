"""Public-portfolio gate for experimental features.

Video generation, the media DB API, Optimus self-evolution and the self-correction loop are still
experimental. They stay off (404, hidden from the UI and /docs) unless the
deployment sets ``KARRY_EXPERIMENTAL_FEATURES=1``, e.g. on a sandbox service.
The novel-to-webtoon pipeline can be published on its own with ``KARRY_WEBTOON=1``.
"""
from __future__ import annotations

import os

from starlette.responses import JSONResponse

WEBTOON_PREFIX = "/api/webtoon"
EXPERIMENTAL_PREFIXES = ("/api/video", "/api/media", "/api/optimus", "/api/agent/self-correction", WEBTOON_PREFIX)


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def experimental_enabled() -> bool:
    return _flag("KARRY_EXPERIMENTAL_FEATURES")


def webtoon_enabled() -> bool:
    return experimental_enabled() or _flag("KARRY_WEBTOON")


def is_experimental_path(path: str) -> bool:
    return any(_under(path, prefix) for prefix in EXPERIMENTAL_PREFIXES)


def path_enabled(path: str) -> bool:
    if not is_experimental_path(path) or experimental_enabled():
        return True
    return _under(path, WEBTOON_PREFIX) and webtoon_enabled()


def public_features() -> dict[str, bool]:
    return {"experimental": experimental_enabled(), "webtoon": webtoon_enabled()}


class ExperimentalFeatureGate:
    """Answer 404 for disabled experimental routes before any other middleware runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and not path_enabled(scope.get("path", "")):
            return await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
        return await self.app(scope, receive, send)
