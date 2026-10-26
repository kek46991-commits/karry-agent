"""Public-portfolio gate for experimental features.

Video generation, the media DB API and Optimus self-evolution are still
experimental. They stay off (404, hidden from the UI and /docs) unless the
deployment sets ``KARRY_EXPERIMENTAL_FEATURES=1``, e.g. on a sandbox service.
"""
from __future__ import annotations

import os

from starlette.responses import JSONResponse

EXPERIMENTAL_PREFIXES = ("/api/video", "/api/media", "/api/optimus")


def experimental_enabled() -> bool:
    return os.getenv("KARRY_EXPERIMENTAL_FEATURES", "").strip().lower() in {"1", "true", "yes", "on"}


def is_experimental_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in EXPERIMENTAL_PREFIXES)


def public_features() -> dict[str, bool]:
    return {"experimental": experimental_enabled()}


class ExperimentalFeatureGate:
    """Answer 404 for experimental routes before any other middleware runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and not experimental_enabled() and is_experimental_path(scope.get("path", "")):
            return await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
        return await self.app(scope, receive, send)
