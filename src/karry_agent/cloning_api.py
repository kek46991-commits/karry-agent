"""Owner-only API for creating isolated GitHub/Render sandbox clones."""
from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .cloning import CloneConfigurationError, CloneProviderError, CloneService

NO_STORE = {"Cache-Control": "no-store"}


def require_owner(
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.getenv("KARRY_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is not configured")
    scheme, _, bearer = (authorization or "").partition(" ")
    supplied = x_owner_token or (bearer.strip() if scheme.lower() == "bearer" else "")
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid owner token",
                            headers={"WWW-Authenticate": "Bearer"})


class CloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=24, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")
    base_branch: str | None = Field(default=None, min_length=1, max_length=80,
                                    pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")
    provision_render: bool = False
    confirm_resource_creation: bool = False


def get_clone_service() -> CloneService:
    try:
        return CloneService.from_env()
    except CloneConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers=NO_STORE) from exc


def _provider_status(error: CloneProviderError) -> int:
    if error.code in {"branch_already_exists", "render_service_conflict"}:
        return 409
    if error.code == "source_branch_not_found":
        return 404
    return 502


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["agent-cloning"],
                       dependencies=[Depends(require_owner)])

    @router.post("/clone", status_code=201)
    def create_clone(
        body: CloneRequest,
        service: Annotated[CloneService, Depends(get_clone_service)],
    ) -> JSONResponse:
        """Create a new sandbox/<slug> branch; Render provisioning is separate opt-in."""
        try:
            settings = service.settings
            if body.provision_render:
                if not body.confirm_resource_creation:
                    raise HTTPException(status_code=400,
                                        detail="confirm_resource_creation must be true to create a Render service")
                settings.require_render()
            branch = service.create_branch(body.slug, body.base_branch)
        except HTTPException:
            raise
        except CloneConfigurationError as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers=NO_STORE) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc), headers=NO_STORE) from exc
        except CloneProviderError as exc:
            return JSONResponse(status_code=_provider_status(exc), headers=NO_STORE,
                                content={"detail": "GitHub could not create the sandbox branch.",
                                         "error_code": exc.code, "provider": exc.provider})

        result: dict[str, object] = {
            "status": "branch_created",
            "repository": branch.repository,
            "branch": branch.branch,
            "source_sha": branch.sha,
            "branch_url": branch.url,
            "render": None,
        }
        if body.provision_render:
            try:
                result["render"] = service.create_render_service(branch.branch, body.slug)
                result["status"] = "sandbox_created"
            except CloneConfigurationError as exc:
                return JSONResponse(status_code=503, headers=NO_STORE,
                                    content={"detail": str(exc), "error_code": "render_not_configured",
                                             "partial_success": result})
            except CloneProviderError as exc:
                status = _provider_status(exc)
                return JSONResponse(status_code=status, headers=NO_STORE,
                                    content={"detail": "The GitHub branch was created, but Render provisioning failed.",
                                             "error_code": exc.code, "provider": exc.provider,
                                             "partial_success": result})
        return JSONResponse(status_code=201, content=result, headers=NO_STORE)

    return router


router = build_router()
