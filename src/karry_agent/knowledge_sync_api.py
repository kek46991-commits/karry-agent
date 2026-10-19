"""``/api/knowledge/sync-context``: owner-only real-time knowledge sync endpoints."""
from __future__ import annotations

import os
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import task_persistence
from .knowledge_sync_service import MAX_CONTENT_CHARS, ContextType, KnowledgeSyncService, Priority, summary

NO_STORE = {"Cache-Control": "no-store"}


def require_owner(
    authorization: Annotated[str | None, Header()] = None,
    x_owner_token: Annotated[str | None, Header()] = None,
) -> None:
    """Accept ``X-Owner-Token: <token>`` or ``Authorization: Bearer <token>``; fail closed without a token."""
    expected = os.getenv("KARRY_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is not configured")
    scheme, _, bearer = (authorization or "").partition(" ")
    supplied = x_owner_token or (bearer.strip() if scheme.lower() == "bearer" else "")
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid owner token", headers={"WWW-Authenticate": "Bearer"})


class SyncMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updated_by: str = Field(default="Owner", min_length=1, max_length=80)
    priority: Priority = "normal"


class SyncContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_type: ContextType
    source: str = Field(min_length=1, max_length=80, pattern=r"^[\w][\w .:/@-]*$")
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    metadata: SyncMetadata = Field(default_factory=SyncMetadata)


def get_sync_service() -> KnowledgeSyncService:
    return KnowledgeSyncService()


router = APIRouter(prefix="/api/knowledge", tags=["knowledge-sync"], dependencies=[Depends(require_owner)])


def _storage_error(exc: task_persistence.TaskStorageError) -> JSONResponse:
    status = 409 if exc.code in {"storage_conflict", "storage_capacity"} else 503
    return JSONResponse(status_code=status, content={"detail": str(exc), "error_code": exc.code}, headers=NO_STORE)


@router.post("/sync-context")
def sync_context(body: SyncContextRequest,
                 service: Annotated[KnowledgeSyncService, Depends(get_sync_service)]) -> JSONResponse:
    """Store or update a context entry; the next agent run uses it in its system prompt / RAG context."""
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="content must not be blank")
    try:
        result = service.sync(body.context_type, body.source, body.content, body.metadata.model_dump())
    except task_persistence.TaskStorageError as exc:
        return _storage_error(exc)
    return JSONResponse(status_code=201 if result["status"] == "created" else 200, content=result, headers=NO_STORE)


@router.get("/contexts")
def list_contexts(service: Annotated[KnowledgeSyncService, Depends(get_sync_service)]) -> JSONResponse:
    try:
        rows: list[dict[str, Any]] = service.store.entries()
    except task_persistence.TaskStorageError as exc:
        return _storage_error(exc)
    return JSONResponse({"contexts": [summary(row) for row in rows]}, headers=NO_STORE)


@router.delete("/contexts/{context_id}")
def delete_context(context_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
                   service: Annotated[KnowledgeSyncService, Depends(get_sync_service)]) -> JSONResponse:
    try:
        found = service.store.delete(context_id)
    except task_persistence.TaskStorageError as exc:
        return _storage_error(exc)
    if not found:
        raise HTTPException(status_code=404, detail="context not found")
    return JSONResponse({"status": "deleted", "id": context_id}, headers=NO_STORE)
