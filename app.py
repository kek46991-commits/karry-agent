"""karry-agent の軽量 FastAPI REST / chat UI shell。"""
from __future__ import annotations

import threading
import uuid
import json
import os
import re
import logging
import secrets
import time
import hashlib
from pathlib import Path
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi import Path as Path_
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator


from karry_agent.calendar_secrets import calendar_secret_status
from karry_agent.core import AgentCore, AgentMetrics, AgentResult
from karry_agent.memory import ConversationMemory
from karry_agent.privacy import PrivateModeSession, has_passphrase
from karry_agent.tools import add_calendar_event, build_default_registry
from karry_agent.agent_core import dispatch_tool
from karry_agent.self_healing import healing_engine, redact
from karry_agent.repair_pipeline import configure_repair_pipeline
from karry_agent import self_correction
from karry_agent import instant_website
from karry_agent import website_artifacts
from karry_agent import website_storage
from karry_agent import task_persistence
from karry_agent import image_attachments
from karry_agent import media_repository, media_generation
from karry_agent import image_generation
from karry_agent import optimus_core
from karry_agent import resource_errors
from karry_agent import knowledge_engine
from karry_agent import knowledge_sync_api
from karry_agent import video_suite_api, video_projects
from karry_agent import feature_flags, chat_video, chat_webtoon
from karry_agent.auth_token import configured_token, token_matches
from karry_agent.secret_masking import mask_secrets, mask_text
from karry_agent import secret_chat, secret_runtime

logger = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(application):
    async with secret_chat.lifespan(application):
        async with video_projects.lifespan(application):
            yield


app = FastAPI(
    title="karry-agent API",
    version="0.2.0",
    description="Gemini Function Calling agent API with chat and execution tracking",
    lifespan=app_lifespan,
)
STATIC_DIR = Path(__file__).parent / "static"
instant_website.configure_static_root(STATIC_DIR)
CHAT_HTML_HEADERS = {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache",
                     "Permissions-Policy": "microphone=(self)"}


class ArtifactNotFound(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=404, detail=resource_errors.missing('artifact')['detail'], headers={"Cache-Control": "no-store"})


ARTIFACT_NOT_FOUND_HINT = resource_errors.missing('artifact')['hint']


def _artifact_published(filename: str) -> bool:
    artifact = instant_website.public_artifact_content(filename)
    if artifact is None:
        return False
    try:
        if not isinstance(artifact, bytes):
            instant_website.read_local_artifact(artifact)
        return True
    except FileNotFoundError:
        return False


def _artifact_response(filename: str, scope: dict):
    try:
        artifact = instant_website.public_artifact_content(filename)
        if artifact is None:
            return resource_errors.respond(scope, resource_errors.missing('artifact'), 404)
        body = artifact if isinstance(artifact, bytes) else instant_website.read_local_artifact(artifact)
        if not body or len(body) > instant_website.MAX_HTML_BYTES:
            raise website_storage.StorageError('保存済み成果物の容量が不正です。', 'storage_integrity')
        body.decode('utf-8')
        response = Response(body, media_type="text/html", headers=instant_website.PUBLIC_HEADERS)
        if scope.get('method') == 'HEAD':
            response.body = b''
        return response
    except StarletteHTTPException as exc:
        return resource_errors.http_failure(scope, exc.status_code, exc.headers)
    except FileNotFoundError:
        return resource_errors.respond(scope, resource_errors.missing('artifact'), 404)
    except website_storage.StorageError as exc:
        healing_engine.analyze_and_heal(exc, {'source': 'artifact_read', 'status_code': 503})
        return resource_errors.respond(scope, resource_errors.failed('artifact'), 503)
    except (OSError, UnicodeError) as exc:
        healing_engine.analyze_and_heal(exc, {'source': 'artifact_read', 'status_code': 503})
        return resource_errors.respond(scope, resource_errors.failed('artifact'), 503)
    except Exception as exc:
        healing_engine.analyze_and_heal(exc, {'source': 'artifact_read', 'status_code': 500})
        return resource_errors.respond(scope, resource_errors.failed('artifact', 500), 500)


@app.api_route("/static/instant/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
def instant_artifact(filename: str, request: Request):
    return _artifact_response(filename, request.scope)


class ArtifactStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        parts = Path(os.path.normpath(path)).parts
        if parts and parts[0] == "instant":
            if len(parts) != 2:
                return resource_errors.respond(scope, resource_errors.missing('artifact'), 404)
            if scope.get("method") not in {"GET", "HEAD"}:
                raise HTTPException(status_code=405, detail="method not allowed")
            return await run_in_threadpool(_artifact_response, parts[1], scope)
        response = await super().get_response(path, scope)
        target = getattr(response, "path", None)
        if target is not None and Path(target).resolve().is_relative_to(instant_website.INSTANT_DIR.resolve()):
            return await run_in_threadpool(_artifact_response, Path(target).resolve().name, scope)
        if parts == ("index.html",) or (target is not None and Path(target).resolve() == (STATIC_DIR / "index.html").resolve()):
            response.headers.update(CHAT_HTML_HEADERS)
        return response


app.mount("/static", ArtifactStaticFiles(directory=STATIC_DIR), name="static")


class ImageUploadGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path", "").rstrip("/") not in {"/api/upload_image", "/api/media/upload", "/api/video/jobs", "/api/video/autopilot"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        token = configured_token()
        scheme, _, supplied = headers.get(b"authorization", b"").strip().partition(b" ")
        if not token or scheme.lower() != b"bearer" or not token_matches(supplied, token):
            status = 503 if not token else 401
            return await JSONResponse({"detail": "KARRY_API_TOKEN is not configured" if not token else "Invalid bearer token"}, status_code=status)(scope, receive, send)
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Content-Length is invalid"}, status_code=400)(scope, receive, send)
        if length < 0 or length > image_attachments.FORM_BODY_LIMIT:
            return await JSONResponse({"detail": "画像リクエストの容量上限を超えています。"}, status_code=413)(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > image_attachments.FORM_BODY_LIMIT:
                return await JSONResponse({"detail": "画像リクエストの容量上限を超えています。"}, status_code=413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        consumed = False
        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, bounded_receive, send)


app.add_middleware(ImageUploadGuard)
app.add_middleware(secret_runtime.SecretChatPrivacyMiddleware)

try:
    _REPAIR_PIPELINE = configure_repair_pipeline(healing_engine, Path(__file__).parent)
except Exception as exc:
    _REPAIR_PIPELINE = None
    healing_engine.analyze_and_heal(exc, {"source": "repair_configuration"})

try:
    _SELF_CORRECTION = self_correction.configure_self_correction(healing_engine, Path(__file__).parent)
except Exception as exc:
    _SELF_CORRECTION = None
    healing_engine.analyze_and_heal(exc, {"source": "repair_configuration"})


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if request.scope.get("secret_chat"):
        return JSONResponse(status_code=500, content={"detail": "シークレットの応答処理に失敗しました。", "isSecret": True}, headers={"Cache-Control": "private, no-store"})
    healing = healing_engine.analyze_and_heal(exc, {"source": "api", "method": request.method,
                                                  "path": request.url.path, "direction": "inbound"})
    if resource_errors.is_retrieval(request.url.path, request.method):
        payload = resource_errors.failed(resource_errors.kind(request.url.path), 500)
        payload['healing_status'] = healing
        return resource_errors.respond(request.scope, payload, 500)
    return JSONResponse(status_code=500, content={
        "detail": "Internal Agent Error Handled by Self-Healing Core", "healing_status": healing})


@app.exception_handler(ArtifactNotFound)
async def artifact_not_found(request: Request, exc: ArtifactNotFound) -> Response:
    return resource_errors.respond(request.scope, resource_errors.missing('artifact'), 404)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if request.scope.get("secret_chat"):
        message = "APIトークンを確認してください。" if exc.status_code == 401 else "シークレット要求を処理できません。入力・実行状態・サーバー設定を確認してください。"
        return JSONResponse(status_code=exc.status_code, content={"detail": message, "isSecret": True}, headers={**(exc.headers or {}), "Cache-Control": "private, no-store"})
    if exc.status_code == 404:
        return resource_errors.respond(request.scope, resource_errors.missing(resource_errors.kind(request.url.path)), 404)
    if resource_errors.is_retrieval(request.url.path, request.method) and exc.status_code >= 500:
        healing_engine.analyze_and_heal(exc, {'source': 'retrieval_http', 'status_code': exc.status_code})
        return resource_errors.http_failure(request.scope, exc.status_code, exc.headers)
    content = {"detail": redact(exc.detail)}
    if exc.status_code == 401 or exc.status_code >= 500:
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        content["healing_status"] = healing_engine.analyze_and_heal(cause, {
            "source": "api_http", "method": request.method, "path": request.url.path,
            "direction": "inbound", "status_code": exc.status_code})
        if exc.status_code == 500 and exc.__cause__ is not None:
            content["detail"] = "agent execution failed"
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.scope.get("secret_chat"):
        return JSONResponse(status_code=422, content={"detail": "入力またはシークレット指定の形式が不正です。", "isSecret": True}, headers={"Cache-Control": "private, no-store"})
    healing_engine.analyze_and_heal(ValueError("request validation failed"),
        {"source": "api_validation", "method": request.method, "path": request.url.path})
    errors = [{"loc": list(error["loc"]), "type": error["type"], "msg": error["msg"]}
              for error in exc.errors()[:20]]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(task_persistence.TaskStorageError)
async def task_storage_error(request: Request, exc: task_persistence.TaskStorageError):
    status = 404 if exc.code == "not_found" else 409 if exc.code in {"session_busy", "storage_conflict", "storage_capacity"} else 503
    if status == 404:
        return resource_errors.respond(request.scope, resource_errors.missing('session', exc.code), 404)
    content = {"detail": str(exc), "error_code": exc.code}
    diagnostic = getattr(exc, "diagnostic_code", None)
    if diagnostic in website_storage.DIAGNOSTIC_HINTS:
        content.update(diagnostic_code=diagnostic, hint=website_storage.DIAGNOSTIC_HINTS[diagnostic])
    return JSONResponse(status_code=status, content=content, headers={"Cache-Control": "no-store"})


@app.exception_handler(image_attachments.ImageAttachmentError)
async def image_attachment_error(request: Request, exc: image_attachments.ImageAttachmentError):
    if exc.status_code == 404:
        return resource_errors.respond(request.scope, resource_errors.missing('image'), 404)
    return JSONResponse({"detail": str(exc)}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})


@app.exception_handler(optimus_core.OptimusError)
async def optimus_error(request: Request, exc: optimus_core.OptimusError):
    return JSONResponse({"detail": str(exc), "error_code": exc.code}, status_code=exc.status_code,
                        headers={"Cache-Control": "no-store"})


ImageId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class GenerateImageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=2000)
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)

    @field_validator("prompt")
    @classmethod
    def clean_generation_prompt(cls, value: str) -> str:
        value = task_persistence.credential_safe(value.strip())
        if not value:
            raise ValueError("画像生成のプロンプトを入力してください。")
        return value


@app.exception_handler(media_repository.MediaError)
async def media_error(request: Request, exc: media_repository.MediaError):
    if exc.status_code == 404:
        return resource_errors.respond(request.scope, resource_errors.missing('image', exc.code), 404)
    return JSONResponse({"detail": str(exc), "error_code": exc.code}, status_code=exc.status_code,
                        headers={"Cache-Control": "no-store"})


class SecretRequest(BaseModel):
    is_secret: bool = Field(default=False, strict=True, validation_alias=AliasChoices("is_secret", "isSecret"))
    secret_session_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="before")
    @classmethod
    def validate_secret_flags(cls, value):
        if isinstance(value, dict):
            if "is_secret" in value and "isSecret" in value and value["is_secret"] != value["isSecret"]:
                raise ValueError("シークレット指定が矛盾しています。")
            if value.get("secret_session_id") and not (value.get("is_secret") is True or value.get("isSecret") is True):
                raise ValueError("シークレット会話IDは通常モードでは利用できません。")
        return value


class RunRequest(SecretRequest):
    prompt: str = Field(..., min_length=1, max_length=20_000, description="エージェントへの指示", validation_alias=AliasChoices("prompt", "message"))
    session_id: str | None = Field(default=None, max_length=100, validation_alias=AliasChoices("session_id", "taskId"))
    stream: bool = Field(default=False, description="SSE 形式で実行イベントを返す")
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    image_ids: list[ImageId] | None = Field(default=None, max_length=4)


class AgentTaskRequest(SecretRequest):
    """旧サンプル形式との互換リクエスト。内部では prompt に変換する。"""

    task: str = Field(..., min_length=1, max_length=20_000, description="エージェントへのタスク")
    session_id: str | None = Field(default=None, max_length=100)


class SessionCreate(BaseModel):
    title: str = Field(default="新しいタスク", min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("タスク名は空白・制御文字を含まない文字列にしてください")
        return value


class MessageRequest(SecretRequest):
    session_id: str = Field(min_length=1, max_length=100, validation_alias=AliasChoices("session_id", "taskId"))
    prompt: str = Field(min_length=1, max_length=20_000, validation_alias=AliasChoices("prompt", "message"))
    stream: bool = False
    request_id: str | None = Field(default=None, min_length=1, max_length=100)
    image_ids: list[ImageId] | None = Field(default=None, max_length=4)

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("メッセージを入力してください")
        return value


class InstantWebsitePayload(BaseModel):
    html_content: str = Field(..., min_length=1, max_length=instant_website.MAX_HTML_BYTES)
    filename: str | None = Field(default=None, max_length=200)


class InstantWebsiteConfirmation(BaseModel):
    approval_id: str = Field(..., min_length=32, max_length=32)
    sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    confirmed: bool


class RunResponse(BaseModel):
    run_id: str
    session_id: str | None = None
    status: str
    text: str
    steps: int
    tool_calls: int
    private_mode: bool = False
    is_secret: bool = False
    isSecret: bool = False
    secret_session_id: str | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any]
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    generated_images: list[str] = Field(default_factory=list)


class AgentRunResponse(BaseModel):
    """`/agent/run` の互換レスポンス。実行の詳細も保持する。"""

    status: str
    task: str
    thought_process: str
    next_action: str
    run_id: str
    session_id: str | None = None
    private_mode: bool = False
    is_secret: bool = False
    isSecret: bool = False
    secret_session_id: str | None = None
    steps: int
    tool_calls: int
    trace: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any]


class SessionResponse(BaseModel):
    session_id: str
    history: list[dict[str, Any]]


_RUNS: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, ConversationMemory] = {}
_PRIVATE_SESSIONS: dict[str, PrivateModeSession] = {}
_PENDING_CALENDAR: dict[str, dict[str, Any]] = {}
_SESSION_META: dict[str, dict[str, str]] = {}
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_APPROVALS: dict[str, tuple[str, float]] = {}
_STATE_LOCK = threading.Lock()
_MAX_RUNS = 100
_MAX_SESSIONS = 100


_BEARER = HTTPBearer(auto_error=False)


def require_api_auth(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None) -> None:
    """Preserve existing default API behavior, but require auth when a token or shell is enabled."""
    token = configured_token()
    if not token and task_persistence.mode() == "s3":
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is required for persistent task access")
    if not token and os.getenv("KARRY_ALLOW_SHELL") == "1":
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is required for shell execution")
    if token and (credentials is None or not token_matches(credentials.credentials, token)):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def require_tool_auth(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)] = None) -> None:
    """New direct tool routes always fail closed when no owner token is configured."""
    if not configured_token():
        raise HTTPException(status_code=503, detail="KARRY_API_TOKEN is not configured")
    require_api_auth(credentials)


video_projects.install(app, require_tool_auth)


class ToolRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)


def get_agent() -> AgentCore:
    """依存性注入ポイント。テストや別デプロイ構成で差し替え可能。"""
    try:
        return AgentCore()
    except ValueError as exc:
        # Dependency injection の外側で発生する設定エラーは、FastAPI の
        # 無情報な 500 ではなく、運用者が修正できる 503 として返す。
        healing_engine.analyze_and_heal(exc, {"source": "agent_configuration"})
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _metrics(result: AgentResult, image_api_calls: int = 0) -> dict[str, Any]:
    metrics = asdict(result.metrics)
    if image_api_calls:
        metrics["api_calls"] = metrics.get("api_calls", 0) + image_api_calls
        metrics["image_api_calls"] = image_api_calls
    return metrics


def _image_service(session_id: str) -> image_attachments.AttachmentService:
    repo = task_persistence.repository() if task_persistence.mode() == "s3" else None
    if repo is None:
        with _STATE_LOCK:
            if session_id not in _SESSIONS:
                raise HTTPException(status_code=404, detail="Session not found")
    return image_attachments.AttachmentService(session_id, repo)


def _image_mutation_lock(session_id: str):
    with _STATE_LOCK:
        _allocate_session_locked(session_id)
        lock = _SESSION_LOCKS[session_id]
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="session is already running")
        return lock


def _trace(result: AgentResult) -> list[dict[str, Any]]:
    return [mask_secrets(asdict(event)) for event in result.trace]


def _new_run() -> str:
    run_id = uuid.uuid4().hex
    with _STATE_LOCK:
        if len(_RUNS) >= _MAX_RUNS:
            oldest = next(iter(_RUNS))
            _RUNS.pop(oldest, None)
        _RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trace": [],
        }
    return run_id


def _finish_run(run_id: str, response: RunResponse) -> None:
    with _STATE_LOCK:
        _RUNS[run_id] = response.model_dump()


def _session(session_id: str | None) -> tuple[str | None, ConversationMemory | None]:
    if not session_id:
        return None, None
    with _STATE_LOCK:
        return session_id, _allocate_session_locked(session_id)


def _drop_session_locked(session_id: str) -> None:
    for store in (_SESSIONS, _SESSION_META, _SESSION_LOCKS, _PRIVATE_SESSIONS, _PENDING_CALENDAR):
        store.pop(session_id, None)
    image_attachments.clear_memory_session(session_id)
    with website_artifacts.LOCK:
        website_artifacts.MEMORY.pop(session_id, None)
    for approval, (owner, _) in list(_SESSION_APPROVALS.items()):
        if owner == session_id:
            _SESSION_APPROVALS.pop(approval, None)


def _reset_conversation_locked(session_id: str) -> None:
    """Clearing history also drops context-bound confirmations and private-mode authorization."""
    for store in (_PRIVATE_SESSIONS, _PENDING_CALENDAR):
        store.pop(session_id, None)
    with website_artifacts.LOCK:
        website_artifacts.MEMORY.pop(session_id, None)
    for approval, (owner, _) in list(_SESSION_APPROVALS.items()):
        if owner == session_id:
            _SESSION_APPROVALS.pop(approval, None)


def _allocate_session_locked(session_id: str, title: str = "新しいタスク") -> ConversationMemory:
    if session_id not in _SESSIONS:
        if len(_SESSIONS) >= _MAX_SESSIONS:
            # Preserve legacy bounded eviction, but never evict an active run.
            oldest = next((sid for sid in _SESSIONS if not _SESSION_LOCKS.get(sid, threading.Lock()).locked()), None)
            if oldest is None:
                raise HTTPException(status_code=409, detail="all sessions are busy")
            _drop_session_locked(oldest)
        _SESSIONS[session_id] = ConversationMemory(max_history=10)
    _SESSION_META.setdefault(session_id, {"title": title, "latest_url": "", "latest_run_id": ""})
    _SESSION_LOCKS.setdefault(session_id, threading.Lock())
    return _SESSIONS[session_id]


def _session_detail_locked(session_id: str) -> dict[str, Any]:
    memory = _SESSIONS.get(session_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Session not found")
    meta = _SESSION_META.get(session_id, {"title": "新しいタスク", "latest_url": "", "latest_run_id": ""})
    messages = [{"role": "assistant" if item["role"] == "model" else item["role"], "content": item["content"]}
                for item in memory.get_history()]
    for output, source in zip(messages, memory.get_history()):
        if source.get("images"):
            output["images"] = list(source["images"])
    images = image_attachments.memory_document(session_id)
    run = _RUNS.get(meta.get("latest_run_id", ""))
    snapshot = website_artifacts.verified_document({'session_id':session_id,'latest_url':meta['latest_url'],'website_artifacts':website_artifacts.MEMORY.get(session_id,{})})
    return {"session_id": session_id, "title": meta["title"], "messages": messages, "latest_url": snapshot['latest_url'],
            "last_run": dict(run) if run and run.get("status") != "running" else None,
            "persistence": "durable" if task_persistence.mode() == "s3" else "ephemeral",
            "image_assets": images["images"], "pending_images": [images["images"][key] for key in images["pending_images"]],
            "artifacts": website_artifacts.summaries(snapshot)}


def _restore_task_locked(document: dict[str, Any]) -> None:
    sid = document["session_id"]
    memory = _allocate_session_locked(sid, document["title"])
    memory.history = [{"role": "model" if row["role"] == "assistant" else row["role"], "content": row["content"], **({"images": row["images"]} if row.get("images") else {})} for row in document["messages"][-20:]]
    run = document.get("last_run") or {}
    _SESSION_META[sid] = {"title": document["title"], "latest_url": document["latest_url"], "latest_run_id": run.get("run_id", "")}
    if run.get("run_id") and run["run_id"] not in _RUNS:
        if len(_RUNS) >= _MAX_RUNS:
            _RUNS.pop(next(iter(_RUNS)), None)
        _RUNS[run["run_id"]] = {**run, "session_id": sid}


def _durable_task_details(document: dict[str, Any]) -> dict[str, Any]:
    document = website_artifacts.verified_document(document)
    sid = document["session_id"]
    with _STATE_LOCK:
        run = _RUNS.get((document.get("last_run") or {}).get("run_id", ""))
    return {"session_id": sid, "title": document["title"], "messages": [
        {"role": "assistant" if row["role"] == "model" else row["role"], "content": row["content"], **({"images": row["images"]} if row.get("images") else {})} for row in document["messages"]],
        "latest_url": document["latest_url"], "last_run": run or document.get("last_run"), "persistence": "durable",
        "image_assets": document.get("images", {}), "pending_images": [document.get("images", {})[key] for key in document.get("pending_images", [])],
        "image_uploads_pending": len(document.get("image_uploads", {})), "artifacts": website_artifacts.summaries(document)}


def _request_digest(prompt: str, image_ids: list[str] | None = None) -> str:
    clean = task_persistence.credential_safe(prompt)
    data = json.dumps([clean, image_ids], ensure_ascii=False) if image_ids else clean
    return hashlib.sha256(data.encode()).hexdigest()


def _cached_run(request: RunRequest | MessageRequest, document: dict[str, Any]) -> RunResponse | None:
    stored = document.get("last_run") or {}
    if stored.get("status") == "failed":
        return None
    if not request.request_id or stored.get("request_id_hash") != hashlib.sha256(request.request_id.encode()).hexdigest():
        return None
    ids = request.image_ids if request.image_ids is not None else stored.get("image_ids", [])
    if stored.get("request_digest") != _request_digest(request.prompt, ids):
        raise task_persistence.TaskStorageError("同じrequest_idで異なる入力を再送できません。", "storage_conflict")
    with _STATE_LOCK:
        result = dict(_RUNS.get(stored["run_id"], stored))
    result.update(session_id=request.session_id, private_mode=False)
    return RunResponse.model_validate(result)


def _replay_payload(result: RunResponse, document: dict[str, Any]) -> dict[str, Any]:
    details = _durable_task_details(document)
    details.pop("last_run", None)
    return {**result.model_dump(), **details, "response": result.text, "replayed": True}


def _stream_replay(payload: dict[str, Any]):
    yield _sse("started", {"replayed": True})
    yield _sse("completed", payload)


def _verify_publication(intent: dict[str, Any]) -> bool:
    content = instant_website.public_artifact_content(intent["filename"])
    if content is None:
        return False
    body = content if isinstance(content, bytes) else content.read_bytes()
    return hashlib.sha256(body).hexdigest() == intent["sha256"]


def _remember_website_previews(session_id: str, result: AgentResult) -> list[dict]:
    artifacts = []
    for event in result.trace:
        observation = event.observation
        if event.action != 'deploy_instant_website' or not isinstance(observation, dict) or not observation.get('confirmation_required'):
            continue
        aid = observation.get('approval_id')
        with instant_website._LOCK:
            plan = instant_website._PENDING.get(aid)
            plan = dict(plan) if plan else None
        if plan is None:
            continue
        artifact = website_artifacts.make(aid, plan)
        if task_persistence.mode() == 's3':
            task_persistence.repository().save_artifact(session_id, artifact)
        else:
            with website_artifacts.LOCK:
                records = dict(website_artifacts.MEMORY.get(session_id, {}))
                records[aid] = artifact
                website_artifacts.validate(records)
                website_artifacts.MEMORY[session_id] = records
        meta = website_artifacts.metadata(artifact, session_id)
        observation.update(artifact_id=aid, artifact=meta, preview_url=meta['preview_url'])
        artifacts.append(meta)
    with _STATE_LOCK:
        now = time.monotonic()
        for key, (sid, expiry) in list(_SESSION_APPROVALS.items()):
            if expiry <= now or sid not in _SESSIONS:
                _SESSION_APPROVALS.pop(key, None)
        for event in result.trace:
            observation = event.observation
            if event.action != "deploy_instant_website" or not isinstance(observation, dict):
                continue
            approval = observation.get("approval_id")
            if observation.get("confirmation_required") and isinstance(approval, str) and approval in instant_website._PENDING:
                _SESSION_APPROVALS[approval] = (session_id, now + instant_website.APPROVAL_TTL)
    return artifacts


def _remember_published_website(session_id: str | None, result: dict[str, Any]) -> None:
    if not session_id or not result.get("success") or result.get("status") != "published":
        return
    url = result.get("public_url")
    if isinstance(url, str) and url.startswith(instant_website.public_base_url() + "/static/instant/"):
        if task_persistence.mode() == "s3":
            try:
                task_persistence.repository().acknowledge_publication(session_id, result["filename"])
                result["history_persistence"] = "durable"
            except task_persistence.TaskStorageError as exc:
                result["history_persistence"] = "failed"
                result["history_error_code"] = exc.code
                result["warning"] = (result.get("warning", "") + " HTMLの公開は成功しましたが、タスクへのURL記録に失敗しました。このURLを控え、再公開せず接続を確認してください。").strip()
                return
        else:
            result["history_persistence"] = "ephemeral"
            with website_artifacts.LOCK:
                for artifact in website_artifacts.MEMORY.get(session_id, {}).values():
                    if artifact['plan']['filename'] == result['filename']:
                        artifact['state'] = 'published'
        with _STATE_LOCK:
            if session_id in _SESSION_META:
                _SESSION_META[session_id]["latest_url"] = url


def _private_mode(session_id: str | None, prompt: str) -> bool:
    """入力またはセッション認証に基づき、現在の実行だけのモードを判定する。"""
    if not session_id:
        return has_passphrase(prompt)
    with _STATE_LOCK:
        auth = _PRIVATE_SESSIONS.setdefault(session_id, PrivateModeSession())
        return auth.authenticate(prompt)


def _calendar_confirmation(prompt: str) -> str | None:
    """明示的な予定登録確認だけを yes / no として判定する。"""
    text = re.sub(r"[\s　。、，！!？?]+", "", prompt.strip().lower())
    if not text:
        return None
    if any(word in text for word in ("キャンセル", "取消", "取り消し", "やめて", "いいえ", "no")):
        return "cancel"
    if text in {"はい", "yes", "y", "ok", "okay", "了解", "承認", "登録", "実行"}:
        return "confirm"
    if any(phrase in text for phrase in ("登録してください", "登録して", "追加してください", "追加して", "実行してください")):
        return "confirm"
    return None


def _pending_calendar_result(session_id: str | None, prompt: str) -> AgentResult | None:
    """保留中の予定に対する明示確認を、モデル任せにせず確実に実行する。"""
    if not session_id:
        return None
    decision = _calendar_confirmation(prompt)
    if decision is None:
        return None
    with _STATE_LOCK:
        pending = dict(_PENDING_CALENDAR.get(session_id, {}))
    if not pending:
        return None
    if decision == "cancel":
        with _STATE_LOCK:
            _PENDING_CALENDAR.pop(session_id, None)
        return AgentResult("Google Calendarへの登録をキャンセルしました。", 1, 0)

    try:
        result = add_calendar_event(**pending, confirmed=True)
    except Exception as exc:
        # Confirmed insert is deliberately outside the model loop, so an API/
        # auth failure must become a normal result instead of a generic 500.
        healing_engine.analyze_and_heal(exc, {"source": "confirmed_tool", "tool_name": "add_calendar_event"})
        return AgentResult(
            f"Google Calendarへの登録に失敗しました（{type(exc).__name__}）。認証状態を確認して再試行してください。",
            1,
            1,
            metrics=AgentMetrics(tool_calls=1, errors=1),
        )
    if result.get("created"):
        with _STATE_LOCK:
            _PENDING_CALENDAR.pop(session_id, None)
        event_id = result.get("event_id") or "不明"
        return AgentResult(f"Google Calendarに登録しました。イベントID: {event_id}", 1, 1, metrics=AgentMetrics(tool_calls=1))
    healing_engine.observe_result(result, {"source": "confirmed_tool", "tool_name": "add_calendar_event"})
    return AgentResult(f"Google Calendarへの登録に失敗しました: {result.get('error', '不明なエラー')}", 1, 1, metrics=AgentMetrics(tool_calls=1, errors=1))


def _remember_calendar_preview(session_id: str | None, result: AgentResult) -> None:
    """モデルが返したCalendarプレビューを次の確認入力に結び付ける。"""
    if not session_id:
        return
    for event in result.trace:
        if event.action != "add_calendar_event" or not isinstance(event.observation, dict):
            continue
        observation = event.observation
        with _STATE_LOCK:
            if observation.get("confirmation_required"):
                args = event.arguments
                _PENDING_CALENDAR[session_id] = {
                    "summary": args.get("summary", ""),
                    "start_time": args.get("start_time", ""),
                    "duration_minutes": args.get("duration_minutes", 60),
                }
                for key in ("end_time", "description"):
                    if key in args:
                        _PENDING_CALENDAR[session_id][key] = args[key]
                preview = observation.get("event_preview", {})
                if preview.get("start", {}).get("dateTime") and preview.get("end", {}).get("dateTime"):
                    _PENDING_CALENDAR[session_id]["start_time"] = preview["start"]["dateTime"]
                    _PENDING_CALENDAR[session_id]["end_time"] = preview["end"]["dateTime"]
            elif observation.get("created"):
                _PENDING_CALENDAR.pop(session_id, None)
        break


def _run_agent(agent: AgentCore, prompt: str, private_mode: bool, current_prompt: str | None = None, images: list[dict] | None = None) -> AgentResult:
    # 既存の差し替え用 FakeAgent（run(prompt) のみ）との互換性を保つ。
    if isinstance(agent, AgentCore):
        if images:
            return agent.run(prompt, private_mode=private_mode, current_prompt=current_prompt, images=images)
        return agent.run(prompt, private_mode=private_mode, current_prompt=current_prompt)
    if images:
        return agent.run(prompt, private_mode=private_mode, images=images)
    if private_mode:
        return agent.run(prompt, private_mode=True)
    return agent.run(prompt)


def _execute_secret(request: RunRequest, agent: AgentCore, *, task_mode: bool = False):
    if not configured_token():
        raise HTTPException(status_code=503, detail="シークレットには所有者認証の設定が必要です。")
    if request.image_ids:
        raise HTTPException(status_code=400, detail="シークレットでは画像保存を行わないため、テキストだけを送信してください。")
    request.session_id = request.session_id or uuid.uuid4().hex
    with secret_runtime.scope():
        try:
            with secret_chat.store.claim(request.session_id, request.secret_session_id) as (sid, entry):
                cached = entry.replay(request.request_id, request.prompt)
                if cached is not None:
                    return {**cached, "replayed": True} if task_mode else RunResponse.model_validate(cached)
                prompt = request.prompt
                if entry.history:
                    context = "\n".join(f"{row['role']}: {row['content']}" for row in entry.history[-6:])
                    prompt = f"シークレット内の会話履歴:\n{context}\n\n今回の依頼:\n{request.prompt}"
                private_mode = entry.private.authenticate(request.prompt)
                run = getattr(agent, "run_secret", None)
                if not callable(run):
                    raise HTTPException(status_code=503, detail="シークレット生成に対応するモデル実装が必要です。")
                try:
                    result = run(prompt, private_mode=private_mode)
                    if result.status != "completed" or not isinstance(result.text, str) or not result.text.strip() or len(result.text) > 100_000:
                        raise ValueError("invalid secret response")
                except Exception:
                    raise HTTPException(status_code=502, detail="シークレットの返答を生成できませんでした。") from None
                response = RunResponse(run_id=uuid.uuid4().hex, session_id=request.session_id,
                    status="completed", text=mask_text(result.text), steps=result.steps, tool_calls=0,
                    private_mode=private_mode, metrics=_metrics(result), is_secret=True, isSecret=True,
                    secret_session_id=sid)
                payload = entry.complete(request.request_id, request.prompt, {**response.model_dump(),
                    "response": response.text, "title": "シークレットチャット", "persistence": "none",
                    "latest_url": "", "pending_images": [], "image_assets": {}})
                return payload if task_mode else response
        except secret_chat.SecretChatError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


def _execute(request: RunRequest, agent: AgentCore, *, task_mode: bool = False) -> RunResponse | dict[str, Any]:
    if request.is_secret:
        return _execute_secret(request, agent, task_mode=task_mode)
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
    repo = task_persistence.repository() if task_persistence.mode() == "s3" else None
    if repo is not None and (not task_mode or request.request_id):
        try:
            existing = repo.load(request.session_id)
            cached = _cached_run(request, existing)
            if cached is not None:
                return _replay_payload(cached, existing) if task_mode else cached
        except task_persistence.TaskStorageError as exc:
            if exc.code != "not_found" or task_mode:
                raise
            repo.create(request.session_id, "新しいタスク")
    with _STATE_LOCK:
        if task_mode and repo is None and request.session_id not in _SESSIONS:
            raise HTTPException(status_code=404, detail="Session not found")
        _allocate_session_locked(request.session_id)
        lock = _SESSION_LOCKS[request.session_id]
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="session is already running")
    lease_id = uuid.uuid4().hex
    claimed = None
    saved = False
    response = None
    try:
        if repo is not None:
            claimed = repo.begin(request.session_id, lease_id)
            cached = _cached_run(request, claimed)
            if cached is not None:
                return _replay_payload(cached, claimed) if task_mode else cached
            with _STATE_LOCK:
                _restore_task_locked(claimed)
        service = image_attachments.AttachmentService(request.session_id, repo)
        image_document = claimed if repo else service.document()
        selected = list(request.image_ids if request.image_ids is not None else image_document.get("pending_images", []))
        images = service.resolve(selected, document=image_document)
        request.image_ids = selected

        def store_generated(body: bytes, mime_type: str, filename: str) -> dict[str, Any]:
            return service.store_generated(image_attachments.normalize(body, mime_type), filename, lease_id if repo is not None else None)

        with image_generation.generation_context(store_generated):
            response = _execute_locked(request, agent, images=images)
        if repo is not None:
            messages = [dict(row) for row in claimed["messages"]] + [
                {"role": "user", "content": request.prompt, **({"images": selected} if selected else {})}, {"role": "model", "content": response.text, **({"images": response.generated_images} if response.generated_images else {})}]
            persisted_run = response.model_dump()
            persisted_run["image_ids"] = selected
            if request.request_id:
                persisted_run.update(request_id_hash=hashlib.sha256(request.request_id.encode()).hexdigest(), request_digest=_request_digest(request.prompt, selected))
            document = repo.finish(request.session_id, lease_id, messages, persisted_run)
            saved = True
            with _STATE_LOCK:
                _restore_task_locked(document)
        elif response.status in {"completed", "needs_user_action"}:
            with image_attachments._MEMORY_LOCK:
                image_document["pending_images"] = [key for key in image_document.get("pending_images", []) if key not in selected]
        if task_mode:
            if repo is not None:
                details = _durable_task_details(document)
            else:
                with _STATE_LOCK:
                    details = _session_detail_locked(request.session_id)
            details.pop("last_run", None)
            return {**response.model_dump(), **details, "response": response.text}
        return response
    except task_persistence.TaskStorageError:
        with _STATE_LOCK:
            if response is not None and response.run_id in _RUNS:
                _RUNS[response.run_id]["status"] = "persistence_failed"
            if claimed is not None:
                _restore_task_locked(claimed)
        raise
    finally:
        if repo is not None and claimed is not None and not saved:
            try:
                repo.release(request.session_id, lease_id)
            except Exception as exc:
                healing_engine.analyze_and_heal(exc, {"source": "task_lease_release"})
        lock.release()


def _chat_video_request(prompt: str) -> bool:
    return feature_flags.experimental_enabled() and chat_video.is_video_request(prompt)


def _chat_webtoon_request(prompt: str) -> bool:
    return feature_flags.webtoon_enabled() and chat_webtoon.is_webtoon_request(prompt)


def _execute_locked(request: RunRequest, agent: AgentCore | None, images: list[dict] | None = None) -> RunResponse:
    run_id = _new_run()
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
    session_id, memory = _session(request.session_id)
    private_mode = _private_mode(session_id, request.prompt)
    prompt = request.prompt
    if memory and memory.get_history():
        context = "\n".join(f"{item['role']}: {item['content']}" for item in memory.get_history()[-6:])
        prompt = f"会話履歴:\n{context}\n\n今回の依頼:\n{request.prompt}"
    try:
        if images and _calendar_confirmation(request.prompt) and session_id in _PENDING_CALENDAR:
            raise ValueError("予定確認時は画像の添付を外してください。")
        result = _pending_calendar_result(session_id, request.prompt)
        if result is None and not private_mode and _chat_webtoon_request(request.prompt):
            result = chat_webtoon.run(_WEBTOON_ROUTER.start_job, request.prompt)
        if result is None and not private_mode and _chat_video_request(request.prompt):
            result = chat_video.run(_VIDEO_ROUTER.start_autopilot, session_id, request.prompt, images, request.request_id)
        if result is None:
            if agent is None:
                agent = app.dependency_overrides.get(get_agent, get_agent)()
            result = _run_agent(agent, prompt, private_mode, current_prompt=request.prompt, images=images)
    except ValueError as exc:
        with _STATE_LOCK:
            _RUNS[run_id]["status"] = "failed"
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        with _STATE_LOCK:
            _RUNS[run_id]["status"] = "failed"
        raise HTTPException(status_code=500, detail="agent execution failed") from exc
    _remember_calendar_preview(session_id, result)
    artifacts = _remember_website_previews(session_id, result)
    generated = image_generation.generated_ids()
    if memory:
        with _STATE_LOCK:
            memory.add_message("user", request.prompt)
            memory.add_message("model", result.text)
            if request.image_ids:
                memory.history[-2]["images"] = list(request.image_ids)
            if generated:
                memory.history[-1]["images"] = generated
    response = RunResponse(
        run_id=run_id,
        session_id=session_id,
        status=getattr(result, "status", "completed"),
        text=mask_text(result.text),
        steps=result.steps,
        tool_calls=result.tool_calls,
        private_mode=private_mode,
        trace=_trace(result),
        artifacts=artifacts,
        metrics=_metrics(result, image_generation.api_calls()),
        generated_images=generated,
    )
    _finish_run(run_id, response)
    with _STATE_LOCK:
        _SESSION_META[session_id]["latest_run_id"] = run_id
    return response


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _stream_chat(request: RunRequest, agent: AgentCore, *, task_mode: bool = False):
    """実行開始後にトレースを SSE として返す最小ストリーミング基盤。"""
    try:
        yield _sse("started", {"isSecret": True} if request.is_secret else {"prompt": mask_text(request.prompt)})
        response = _execute(request, agent, task_mode=task_mode)
        payload = response if isinstance(response, dict) else response.model_dump()
        for index, event in enumerate(payload["trace"], start=1):
            yield _sse("step", {"index": index, **event})
        yield _sse("completed", payload)
    except task_persistence.TaskStorageError as exc:
        status = 404 if exc.code == "not_found" else 409 if exc.code in {"session_busy", "storage_conflict", "storage_capacity"} else 503
        payload = resource_errors.missing('session', exc.code) if status == 404 else {"detail": str(exc), "error_code": exc.code}
        yield _sse("error", {"status_code": status, **payload})
    except image_attachments.ImageAttachmentError as exc:
        yield _sse("error", {"status_code": exc.status_code, "detail": str(exc)})
    except HTTPException as exc:
        if request.is_secret:
            yield _sse("error", {"status_code": exc.status_code, "detail": "シークレットの応答を完了できませんでした。", "isSecret": True})
            return
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        healing = healing_engine.analyze_and_heal(cause, {"source": "api_stream", "status_code": exc.status_code})
        yield _sse("error", {"status_code": exc.status_code, "detail": redact(exc.detail), "healing_status": healing})
    except Exception as exc:
        if request.is_secret:
            yield _sse("error", {"status_code": 500, "detail": "シークレットの応答を完了できませんでした。", "isSecret": True})
            return
        healing = healing_engine.analyze_and_heal(exc, {"source": "api_stream", "operation": "serialize_or_execute"})
        # Never put the failing/cyclic payload or raw exception into the fallback.
        yield _sse("error", {"status_code": 500, "detail": "agent stream failed", "healing_status": healing})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "karry-agent"}


@app.get("/api/features")
def features() -> dict[str, bool]:
    """公開してよい機能フラグだけを返す（認証不要・秘密情報なし）。"""
    return feature_flags.public_features()


@app.get("/health/calendar")
def calendar_health() -> dict[str, Any]:
    """Calendar 用 Secret の有無と形式だけを返す。値は返さない。"""
    status = calendar_secret_status(dict(os.environ))
    return {"ready": all(entry["valid"] for entry in status.values()), "env": status}


@app.get("/", include_in_schema=False)
def index():
    from fastapi.responses import FileResponse

    return FileResponse(STATIC_DIR / "index.html", headers=CHAT_HTML_HEADERS)


@app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_auth)])
def run_agent(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse:
    return _execute(request, agent)


@app.post("/agent/run", response_model=AgentRunResponse, dependencies=[Depends(require_api_auth)])
def run_agent_compat(
    request: AgentTaskRequest,
) -> AgentRunResponse:
    """旧 Jimmy-Core サンプルの task 形式を既存の安全な AgentCore へ接続する。"""
    # Body の min_length 検証を先に完了させるため、この互換ルートだけは
    # AgentCore の依存性を関数内で解決する。テストや別デプロイからの
    # dependency override は従来どおり尊重する。
    agent_factory = app.dependency_overrides.get(get_agent, get_agent)
    agent = agent_factory()
    response = _execute(
        RunRequest(prompt=request.task, session_id=request.session_id, is_secret=request.is_secret, secret_session_id=request.secret_session_id),
        agent,
    )
    return AgentRunResponse(
        status=response.status,
        task=request.task,
        thought_process=response.text,
        next_action="実行完了または次のステップへ移行",
        run_id=response.run_id,
        session_id=response.session_id,
        private_mode=response.private_mode,
        is_secret=response.is_secret,
        isSecret=response.isSecret,
        secret_session_id=response.secret_session_id,
        steps=response.steps,
        tool_calls=response.tool_calls,
        trace=response.trace,
        metrics=response.metrics,
    )


@app.post("/chat", response_model=None, dependencies=[Depends(require_api_auth)])
def chat(request: RunRequest, agent: Annotated[AgentCore, Depends(get_agent)]) -> RunResponse | StreamingResponse:
    """会話履歴を保持しながらエージェントを呼び出す。"""
    if not request.session_id:
        request.session_id = uuid.uuid4().hex
    if request.stream:
        return StreamingResponse(_stream_chat(request, agent), media_type="text/event-stream")
    return _execute(request, agent)


@app.get("/sessions/{session_id}", response_model=SessionResponse, dependencies=[Depends(require_api_auth)])
def get_session(session_id: str) -> SessionResponse:
    if task_persistence.mode() == "s3":
        document = task_persistence.repository().load(session_id)
        return SessionResponse(session_id=session_id, history=document["messages"])
    with _STATE_LOCK:
        memory = _SESSIONS.get(session_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(session_id=session_id, history=memory.get_history())


@app.post("/api/sessions", dependencies=[Depends(require_tool_auth)])
def create_session(data: SessionCreate):
    if task_persistence.mode() == "s3":
        sid = uuid.uuid4().hex
        document = task_persistence.repository().create(sid, data.title)
        return JSONResponse({"session_id": sid, "title": document["title"], "persistence": "durable"}, headers={"Cache-Control": "no-store"})
    with _STATE_LOCK:
        if len(_SESSIONS) >= _MAX_SESSIONS:
            raise HTTPException(status_code=409, detail="session capacity reached")
        session_id = uuid.uuid4().hex
        if session_id in _SESSIONS:
            raise HTTPException(status_code=409, detail="session id collision; retry")
        _allocate_session_locked(session_id, data.title)
    return JSONResponse({"session_id": session_id, "title": data.title, "persistence": "ephemeral"}, headers={"Cache-Control": "no-store"})


@app.get("/api/agent/tasks/storage", dependencies=[Depends(require_tool_auth)])
def task_storage_connection():
    """Read-only, owner-only: format validity must not imply working credentials."""
    result = {"configuration_valid": False, "connection_status": "not_checked"}
    try:
        mode = task_persistence.mode()
        result.update(mode=mode, durable=mode == "s3")
        if mode == "memory":
            return JSONResponse({**result, "configuration_valid": True, "connection_status": "ephemeral"},
                                headers={"Cache-Control": "no-store"})
        repo = task_persistence.repository()
        from urllib.parse import urlsplit
        result.update(configuration_valid=True, target={"bucket": repo.config.bucket,
            "endpoint_host": urlsplit(repo.config.endpoint).hostname if repo.config.endpoint else "aws-s3",
            "region": repo.config.region, "task_prefix": repo.prefix})
        rows = repo.list()
        return JSONResponse({**result, "connection_status": "connected", "tasks_count": len(rows)},
                            headers={"Cache-Control": "no-store"})
    except task_persistence.TaskStorageError as exc:
        diagnostic = getattr(exc, "diagnostic_code", None)
        safe = diagnostic if diagnostic in website_storage.DIAGNOSTIC_HINTS else "storage_unknown"
        return JSONResponse(status_code=503, content={**result, "connection_status": "failed",
            "error_code": exc.code, "diagnostic_code": safe, "hint": website_storage.DIAGNOSTIC_HINTS[safe],
            "detail": str(exc)}, headers={"Cache-Control": "no-store"})


class KnowledgeSyncRequest(BaseModel):
    topic: str = Field(default="typescript-agent", max_length=60)


def _knowledge_error(exc: knowledge_engine.KnowledgeError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc), "error_code": exc.code},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/knowledge/sync", dependencies=[Depends(require_tool_auth)])
def sync_knowledge(body: KnowledgeSyncRequest | None = None):
    """GitHubの人気リポジトリからREADMEを取得し、知識として保存する。LLMの生成は呼ばない。"""
    try:
        result = knowledge_engine.KnowledgeAcquisitionEngine().ingest((body or KnowledgeSyncRequest()).topic)
    except knowledge_engine.KnowledgeError as exc:
        return _knowledge_error(exc)
    return JSONResponse({"message": "Autonomous knowledge acquisition process completed.", **result},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/knowledge", dependencies=[Depends(require_tool_auth)])
def list_knowledge():
    rows = knowledge_engine.KnowledgeStore().entries()
    return JSONResponse({"entries": [knowledge_engine.summary(row) for row in rows]},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/knowledge/search", dependencies=[Depends(require_tool_auth)])
def search_knowledge_api(q: Annotated[str, Query(min_length=1, max_length=500)],
                     limit: Annotated[int, Query(ge=1, le=5)] = 3):
    results = knowledge_engine.KnowledgeAcquisitionEngine().search(q, limit)
    return JSONResponse({"notice": knowledge_engine.UNTRUSTED_NOTICE, "results": results},
                        headers={"Cache-Control": "no-store"})


app.include_router(knowledge_sync_api.router)


@app.get("/api/sessions", dependencies=[Depends(require_tool_auth)])
def list_sessions():
    if task_persistence.mode() == "s3":
        return JSONResponse(task_persistence.repository().list(), headers={"Cache-Control": "no-store"})
    with _STATE_LOCK:
        rows = [{"session_id": sid, "title": _SESSION_META.get(sid, {}).get("title", "新しいタスク")} for sid in _SESSIONS]
    return JSONResponse(rows, headers={"Cache-Control": "no-store"})


@app.get("/api/sessions/{session_id}", dependencies=[Depends(require_tool_auth)])
def task_session(session_id: str):
    if task_persistence.mode() == "s3":
        document = task_persistence.repository().reconcile(session_id, _verify_publication)
        document = _image_service(session_id).reconcile(document)
        return JSONResponse(_durable_task_details(document), headers={"Cache-Control": "no-store"})
    with _STATE_LOCK:
        details = _session_detail_locked(session_id)
    return JSONResponse(details, headers={"Cache-Control": "no-store"})


@app.delete("/api/sessions/{session_id}", dependencies=[Depends(require_tool_auth)])
def delete_task_session(session_id: str):
    """タスクと会話・画像・未公開プレビューを完全に削除する。公開済みHTMLは公開先に残る。"""
    with _STATE_LOCK:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is not None and not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="session is already running")
    try:
        if task_persistence.mode() == "s3":
            task_persistence.repository().delete(session_id)
        elif session_id not in _SESSIONS:
            raise HTTPException(status_code=404, detail="Session not found")
        with _STATE_LOCK:
            _drop_session_locked(session_id)
            for run_id, run in list(_RUNS.items()):
                if run.get("session_id") == session_id:
                    _RUNS.pop(run_id, None)
    finally:
        if lock is not None:
            lock.release()
    return JSONResponse({"session_id": session_id, "deleted": True}, headers={"Cache-Control": "no-store"})


@app.delete("/api/sessions/{session_id}/messages", dependencies=[Depends(require_tool_auth)])
def clear_task_history(session_id: str):
    """会話履歴とモデルへ渡す文脈を消去する。タスク自体・公開済みURL・次回添付画像は残す。"""
    if task_persistence.mode() == "s3":
        with _STATE_LOCK:
            lock = _SESSION_LOCKS.get(session_id)
            if lock is not None and lock.locked():
                raise HTTPException(status_code=409, detail="session is already running")
        document = task_persistence.repository().clear_history(session_id)
        with _STATE_LOCK:
            _reset_conversation_locked(session_id)
            _restore_task_locked(document)
        return JSONResponse(_durable_task_details(document), headers={"Cache-Control": "no-store"})
    with _STATE_LOCK:
        if session_id not in _SESSIONS:
            raise HTTPException(status_code=404, detail="Session not found")
        lock = _SESSION_LOCKS[session_id]
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="session is already running")
        try:
            _SESSIONS[session_id].clear()
            _SESSION_META[session_id]["latest_run_id"] = ""
            _reset_conversation_locked(session_id)
            details = _session_detail_locked(session_id)
        finally:
            lock.release()
    return JSONResponse(details, headers={"Cache-Control": "no-store"})


def _media_session(session_id: str) -> None:
    # Existing task authorization/existence rules; do not silently create tasks.
    _image_service(session_id).document()


_VIDEO_ROUTER = video_suite_api.build_router(require_tool_auth, lambda session_id: _media_session(session_id))
app.include_router(_VIDEO_ROUTER)

from karry_agent.webtoon import api as webtoon_api  # noqa: E402

_WEBTOON_ROUTER = webtoon_api.build_router(require_tool_auth)
app.include_router(_WEBTOON_ROUTER)


def _media_json(value: Any, status_code: int = 200):
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(jsonable_encoder(value), status_code=status_code, headers={"Cache-Control": "no-store"})


def _job_payload(repo, job):
    from urllib.parse import urlencode
    state = job["state"]
    result = {"status": state, "job_id": job["id"], "request_id": job["request_id"],
              "session_id": job["session_id"], "type": "image_generated", "error_code": job["error_code"]}
    if state == "succeeded":
        result["media_id"] = job["id"]
        result["file_url"] = "/api/media/" + str(job["id"]) + "?" + urlencode({"session_id": job["session_id"]})
    return result


@app.post("/api/media/upload", dependencies=[Depends(require_tool_auth)])
async def media_upload(session_id: Annotated[str, Form(min_length=1, max_length=100)], file: Annotated[UploadFile, File()]):
    try:
        content = await file.read(image_attachments.MAX_UPLOAD_BYTES + 1)
        def store():
            _media_session(session_id)
            image = image_attachments.normalize(content, file.content_type or "")
            row = media_repository.MediaRepository().insert_upload(session_id, image)
            return {**row, "status": "success", "filename": task_persistence.credential_safe(file.filename or "image")[:120], "warning": image.warning}
        return _media_json(await run_in_threadpool(store))
    finally:
        await file.close()


@app.post("/api/media/generate", dependencies=[Depends(require_tool_auth)])
def media_generate(request: GenerateImageRequest):
    _media_session(request.session_id)
    if os.getenv("KARRY_MEDIA_GENERATION_ENABLED") != "1":
        raise media_repository.MediaError("画像生成は未有効化です。管理者がキーと利用設定を確認してください。", code="generation_disabled")
    client = media_generation.ReplicateClient()
    repo = media_repository.MediaRepository()
    job, created = repo.begin_job(request.session_id, request.request_id, request.prompt)
    if created:
        try:
            prediction = client.create(request.prompt)
            job = repo.set_submitted(job["id"], prediction["id"])
        except media_repository.MediaError as exc:
            # An ambiguous POST is never replayed or claimed as a completed image.
            if exc.code == "submission_rejected":
                repo.reject_submission(job["id"])
            else:
                repo.mark_unknown(job["id"])
            job = repo.job(request.session_id, job["id"])
            return _media_json(_job_payload(repo, job), 200 if job["state"] == "failed" else 202)
        job = media_generation.refresh_job(repo, job, client, prediction)
    payload = _job_payload(repo, job)
    return _media_json(payload, 200 if job["state"] in {"succeeded", "failed", "canceled"} else 202)


@app.get("/api/media/jobs/{job_id}", dependencies=[Depends(require_tool_auth)])
def media_job(job_id: uuid.UUID, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    _media_session(session_id)
    repo = media_repository.MediaRepository()
    job = repo.job(session_id, job_id)
    if job["state"] == "processing":
        job = media_generation.refresh_job(repo, job, media_generation.ReplicateClient())
    return _media_json(_job_payload(repo, job))


@app.get("/api/media", dependencies=[Depends(require_tool_auth)])
def media_list(session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    from urllib.parse import urlencode
    _media_session(session_id)
    rows = media_repository.MediaRepository().list_media(session_id)
    for row in rows:
        row["file_url"] = "/api/media/" + str(row["id"]) + "?" + urlencode({"session_id": session_id})
    return _media_json(rows)


@app.get("/api/media/{media_id}", dependencies=[Depends(require_tool_auth)])
def media_file(media_id: uuid.UUID, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    _media_session(session_id)
    repo = media_repository.MediaRepository()
    row = repo.media(session_id, media_id)
    media_repository.stored_image(row)
    return _media_json(row)


@app.post("/api/upload_image", dependencies=[Depends(require_tool_auth)])
async def upload_image(session_id: Annotated[str, Form(min_length=1, max_length=100)], file: Annotated[UploadFile, File()]):
    try:
        contents = await file.read(image_attachments.MAX_UPLOAD_BYTES + 1)
        def upload():
            service = _image_service(session_id)
            service.document()
            lock = _image_mutation_lock(session_id)
            try:
                image = image_attachments.normalize(contents, file.content_type or "")
                meta = service.upload(image, file.filename or "image")
                return {"status": "success", "filename": meta["filename"], "image_id": meta["id"],
                    "image_url": image_attachments.data_uri(image.body, image.mime_type), "session_id": session_id,
                    "image": meta, "warning": image.warning}
            finally:
                lock.release()
        result = await run_in_threadpool(upload)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    finally:
        await file.close()


@app.get("/api/images/{image_id}", dependencies=[Depends(require_tool_auth)])
def image_preview(image_id: str, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    meta, body = _image_service(session_id).get(image_id)
    return JSONResponse({"image_url": image_attachments.data_uri(body, meta["mime_type"]), "image": meta}, headers={"Cache-Control": "no-store"})


@app.delete("/api/pending_images/{image_id}", dependencies=[Depends(require_tool_auth)])
def detach_image(image_id: str, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    service = _image_service(session_id)
    service.document()
    lock = _image_mutation_lock(session_id)
    try:
        service.detach(image_id)
        return JSONResponse({"status": "detached", "image_id": image_id, "session_id": session_id}, headers={"Cache-Control": "no-store"})
    finally:
        lock.release()


@app.post("/api/chat", response_model=None, dependencies=[Depends(require_tool_auth)])
def task_chat(request: MessageRequest):
    if request.is_secret:
        agent_factory = app.dependency_overrides.get(get_agent, get_agent)
        agent = agent_factory()
        run_request = RunRequest(prompt=request.prompt, session_id=request.session_id, stream=request.stream,
            request_id=request.request_id, image_ids=request.image_ids, is_secret=True, secret_session_id=request.secret_session_id)
        if request.stream:
            return StreamingResponse(_stream_chat(run_request, agent, task_mode=True), media_type="text/event-stream", headers={"Cache-Control": "private, no-store"})
        return JSONResponse(_execute(run_request, agent, task_mode=True), headers={"Cache-Control": "private, no-store"})
    if task_persistence.mode() == "s3":
        document = task_persistence.repository().load(request.session_id)
        if document.get("image_uploads"):
            document = _image_service(request.session_id).reconcile(document)
        cached = _cached_run(request, document)
        if cached is not None:
            payload = _replay_payload(cached, document)
            if request.stream:
                return StreamingResponse(_stream_replay(payload), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
            return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    else:
        with _STATE_LOCK:
            if request.session_id not in _SESSIONS:
                raise HTTPException(status_code=404, detail="Session not found")
    # Do not initialize the model for list/create/detail/unknown-session requests or chat video/webtoon requests.
    agent_factory = app.dependency_overrides.get(get_agent, get_agent)
    agent = None if _chat_webtoon_request(request.prompt) or _chat_video_request(request.prompt) else agent_factory()
    run_request = RunRequest(prompt=request.prompt, session_id=request.session_id, stream=request.stream, request_id=request.request_id, image_ids=request.image_ids)
    if request.stream:
        return StreamingResponse(_stream_chat(run_request, agent, task_mode=True), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
    return JSONResponse(_execute(run_request, agent, task_mode=True), headers={"Cache-Control": "no-store"})


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_auth)])
def get_run(run_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        run = _RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


class SecretForgetRequest(BaseModel):
    secret_session_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@app.post("/api/secret/forget", dependencies=[Depends(require_tool_auth)])
def forget_secret(request: SecretForgetRequest):
    try:
        secret_chat.store.forget(request.secret_session_id)
    except secret_chat.SecretChatError:
        raise HTTPException(status_code=409, detail="処理中のシークレット会話は消去できません。") from None
    return JSONResponse({"success": True, "persistence": "none"}, headers={"Cache-Control": "private, no-store"})


@app.get("/api/agent/tools", dependencies=[Depends(require_tool_auth)])
def agent_tools() -> dict[str, Any]:
    registry = build_default_registry()
    return {"tools": [tool.declaration for tool in registry.tools.values()]}


@app.post("/api/agent/tools/execute", dependencies=[Depends(require_tool_auth)])
def execute_agent_tool(request: ToolRequest) -> dict[str, Any]:
    # Direct authenticated owner calls supply the exact final HTML themselves.
    # Model dispatch instead calls the staged registry wrapper and cannot approve.
    if request.tool_name == "deploy_instant_website":
        if request.session_id:
            if task_persistence.mode() == "s3":
                task_persistence.repository().load(request.session_id)
            else:
                with _STATE_LOCK:
                    if request.session_id not in _SESSIONS:
                        raise HTTPException(status_code=404, detail="Session not found")
        if set(request.arguments) - {"html_content", "filename"}:
            raise HTTPException(status_code=400, detail="invalid publication arguments")
        try:
            payload = InstantWebsitePayload(**request.arguments)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid HTML publication payload") from None
        if request.session_id and task_persistence.mode() == "s3":
            try:
                plan = instant_website._plan(payload.html_content, payload.filename)
                task_persistence.repository().stage_publication(request.session_id, plan)
                result = instant_website._publish(plan)
            except task_persistence.TaskStorageError:
                raise
            except Exception as exc:
                result = instant_website._error(exc)
        else:
            result = instant_website.deploy_instant_website(payload.html_content, payload.filename)
        _remember_published_website(request.session_id, result)
        return {"tool_name": request.tool_name, "result": result}
    return {"tool_name": request.tool_name, "result": mask_secrets(dispatch_tool(request.tool_name, request.arguments))}


@app.post("/api/agent/websites/confirm", dependencies=[Depends(require_tool_auth)])
def confirm_website(request: InstantWebsiteConfirmation) -> dict[str, Any]:
    with _STATE_LOCK:
        owner = _SESSION_APPROVALS.get(request.approval_id)
    if request.confirmed and owner and owner[1] > time.monotonic() and task_persistence.mode() == "s3":
        with instant_website._LOCK:
            plan = instant_website._PENDING.get(request.approval_id)
            plan = dict(plan) if plan and plan["sha256"] == request.sha256 and plan["expires"] > time.monotonic() else None
        if plan:
            task_persistence.repository().stage_publication(owner[0], plan)
    result = instant_website.confirm_instant_website(request.approval_id, request.sha256, request.confirmed)
    if owner and owner[1] > time.monotonic():
        _remember_published_website(owner[0], result)
    with _STATE_LOCK:
        if result.get("status") in {"published", "cancelled"} or request.approval_id not in instant_website._PENDING:
            _SESSION_APPROVALS.pop(request.approval_id, None)
    return result


def _website_record(artifact_id: str, session_id: str) -> dict:
    if not re.fullmatch(r'[a-f0-9]{32}', artifact_id):
        raise HTTPException(404, '指定された下書きが見つかりません。')
    if task_persistence.mode() == 's3':
        document = task_persistence.repository().reconcile(session_id, _verify_publication)
        records = website_artifacts.verified_document(document).get('website_artifacts', {})
    else:
        with _STATE_LOCK:
            if session_id not in _SESSIONS:
                raise HTTPException(404, '指定されたタスクが見つかりません。')
        with website_artifacts.LOCK:
            records = dict(website_artifacts.MEMORY.get(session_id, {}))
        records = website_artifacts.verified_document({'website_artifacts':records}).get('website_artifacts', {})
    row = records.get(artifact_id)
    if row is None:
        raise HTTPException(404, '指定された下書きが見つかりません。')
    return row


@app.get('/api/website-artifacts/{artifact_id}', dependencies=[Depends(require_tool_auth)])
def website_artifact_source(artifact_id: str, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    return JSONResponse(website_artifacts.source(_website_record(artifact_id, session_id), session_id), headers={'Cache-Control':'no-store'})


@app.get('/artifacts/{artifact_id}', dependencies=[Depends(require_tool_auth)])
def website_artifact_preview(artifact_id: str, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    row = _website_record(artifact_id, session_id)
    return Response(row['plan']['html_content'], media_type='text/html', headers=instant_website.PUBLIC_HEADERS)


@app.post('/api/website-artifacts/{artifact_id}/prepare', dependencies=[Depends(require_tool_auth)])
def prepare_saved_website(artifact_id: str, session_id: Annotated[str, Query(min_length=1, max_length=100)]):
    row = _website_record(artifact_id, session_id)
    if row['state'] == 'unavailable':
        raise HTTPException(409, '公開ファイルの実体を確認できません。下書きのプレビューは残っています。公開には新しい成果物として作成し直してください。')
    if row['state'] == 'published':
        return {'success':True,'status':'published','public_url':row['plan']['public_url']}
    result = instant_website.prepare_plan(row['plan'])
    if result.get('confirmation_required'):
        with _STATE_LOCK:
            _SESSION_APPROVALS[result['approval_id']] = (session_id, time.monotonic()+instant_website.APPROVAL_TTL)
        result['artifact_id'] = artifact_id
    return result


@app.get("/api/artifacts/{filename}", dependencies=[Depends(require_tool_auth)])
def artifact_status(filename: str):
    """Report publication state with 200 so clients can check a link before sharing it."""
    if not website_storage.FILENAME_RE.fullmatch(filename):
        return JSONResponse({"filename": filename, "published": False, "error_code": "invalid_filename",
                             "hint": ARTIFACT_NOT_FOUND_HINT}, headers={"Cache-Control": "no-store"})
    try:
        published = _artifact_published(filename)
    except website_storage.StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc), headers={"Cache-Control": "no-store"}) from None
    result = {"filename": filename, "published": published,
              "public_url": instant_website.public_base_url() + "/static/instant/" + filename if published else None}
    if not published:
        result.update(error_code="artifact_not_found", hint=ARTIFACT_NOT_FOUND_HINT)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/agent/websites/storage", dependencies=[Depends(require_tool_auth)])
def website_storage_status(verify: bool = False) -> dict[str, Any]:
    """Owner-only diagnostics. No secrets, no objects are created by this check."""
    try:
        mode = website_storage.storage_mode()
        if mode == "local":
            return {"mode": mode, "configuration_valid": True, "durable": False, "connection_status": "not_applicable"}
        config = website_storage.load_storage_config()
        if verify:
            try:
                website_storage.S3WebsiteStore(config).client.list_objects_v2(Bucket=config.bucket, Prefix=config.prefix, MaxKeys=1)
            except Exception:
                return {"mode": mode, "configuration_valid": True, "durable": True, "connection_status": "failed"}
        return {"mode": mode, "configuration_valid": True, "durable": True, "connection_status": "verified" if verify else "not_checked"}
    except website_storage.StorageError as exc:
        return {"mode": os.getenv("KARRY_WEBSITE_STORAGE", "s3"), "configuration_valid": False, "durable": False,
                "connection_status": "not_checked", "error_code": exc.code, "error": str(exc)}


@app.get("/api/agent/healing", dependencies=[Depends(require_tool_auth)])
def healing_status() -> dict[str, Any]:
    return healing_engine.status()


@app.get("/api/agent/healing/incidents", dependencies=[Depends(require_tool_auth)])
def healing_incidents() -> dict[str, Any]:
    return {"incidents": healing_engine.error_logs}


class SelfCorrectionRequest(BaseModel):
    tests: list[str] | None = Field(default=None, max_length=10)


def _self_correction_loop() -> self_correction.SelfCorrectionLoop:
    if _SELF_CORRECTION is None:
        raise HTTPException(status_code=503, detail="自己修正は無効です（KARRY_SELF_CORRECTION=1 で有効化）。")
    return _SELF_CORRECTION


@app.post("/api/agent/self-correction", status_code=202, dependencies=[Depends(require_tool_auth)])
def start_self_correction(body: SelfCorrectionRequest | None = None) -> dict[str, Any]:
    """隔離コピーでテスト→Gemini修正→書き換え→再実行を回す。本体のファイルは変更しない。"""
    try:
        return _self_correction_loop().submit((body or SelfCorrectionRequest()).tests)
    except self_correction.SelfCorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@app.get("/api/agent/self-correction", dependencies=[Depends(require_tool_auth)])
def list_self_corrections() -> dict[str, Any]:
    return {"jobs": _self_correction_loop().jobs()}


@app.get("/api/agent/self-correction/{job_id}", dependencies=[Depends(require_tool_auth)])
def self_correction_status(job_id: str) -> dict[str, Any]:
    job = _self_correction_loop().job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="self-correction job not found")
    return job


_OPTIMUS = optimus_core.OptimusCore(Path(__file__).parent)


class CloneSpawnRequest(BaseModel):
    mission: str = Field(..., min_length=1, max_length=2000)
    specialist: str = Field(default="general", min_length=1, max_length=50)


class CloneRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20_000)


class EvolutionEdit(BaseModel):
    path: str = Field(..., min_length=1, max_length=200)
    old: str = Field(..., min_length=1, max_length=30_000)
    new: str = Field(..., max_length=30_000)


class EvolutionRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    edits: list[EvolutionEdit] | None = Field(default=None, min_length=1, max_length=3)
    paths: list[str] | None = Field(default=None, min_length=1, max_length=10)


class EvolutionConfirmation(BaseModel):
    approval_id: str = Field(..., pattern=r"^[a-f0-9]{32}$")
    sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    confirmed: bool


class EvolutionDeploy(BaseModel):
    confirmed: bool


@app.get("/api/optimus/status", dependencies=[Depends(require_tool_auth)])
def optimus_status() -> dict[str, Any]:
    return _OPTIMUS.status()


@app.get("/api/optimus/clones", dependencies=[Depends(require_tool_auth)])
def list_clones() -> dict[str, Any]:
    return {"clones": _OPTIMUS.clones.list(), "max_clones": _OPTIMUS.clones.limit()}


@app.post("/api/optimus/clones", status_code=201, dependencies=[Depends(require_tool_auth)])
def spawn_clone(request: CloneSpawnRequest) -> dict[str, Any]:
    return _OPTIMUS.clones.spawn(request.mission, request.specialist)


@app.post("/api/optimus/clones/{clone_id}/run", dependencies=[Depends(require_tool_auth)])
def run_clone(clone_id: str, request: CloneRunRequest, agent: AgentCore = Depends(get_agent)) -> dict[str, Any]:
    return _OPTIMUS.clones.run(clone_id, request.prompt, agent=agent)


@app.delete("/api/optimus/clones/{clone_id}", dependencies=[Depends(require_tool_auth)])
def retire_clone(clone_id: str) -> dict[str, Any]:
    return _OPTIMUS.clones.retire(clone_id)


@app.post("/api/optimus/evolutions", dependencies=[Depends(require_tool_auth)])
def propose_evolution(request: EvolutionRequest) -> dict[str, Any]:
    edits = [edit.model_dump() for edit in request.edits] if request.edits else None
    return _OPTIMUS.propose(request.goal, edits, request.paths)


@app.post("/api/optimus/evolutions/confirm", dependencies=[Depends(require_tool_auth)])
def confirm_evolution(request: EvolutionConfirmation) -> dict[str, Any]:
    return _OPTIMUS.confirm(request.approval_id, request.sha256, request.confirmed)


@app.get("/api/optimus/evolutions/{pr_number}", dependencies=[Depends(require_tool_auth)])
def evolution_status(pr_number: Annotated[int, Path_(ge=1)]) -> dict[str, Any]:
    return _OPTIMUS.evolution_status(pr_number)


@app.post("/api/optimus/evolutions/{pr_number}/deploy", dependencies=[Depends(require_tool_auth)])
def deploy_evolution(pr_number: Annotated[int, Path_(ge=1)], request: EvolutionDeploy) -> dict[str, Any]:
    return _OPTIMUS.deploy(pr_number, request.confirmed)


def _public_openapi() -> dict[str, Any]:
    schema = FastAPI.openapi(app)
    if feature_flags.experimental_enabled():
        return schema
    filtered = dict(schema)
    filtered["paths"] = {path: ops for path, ops in schema.get("paths", {}).items() if feature_flags.path_enabled(path)}
    return filtered


app.openapi = _public_openapi
# Added last so it wraps every other middleware (video/image guards included).
app.add_middleware(feature_flags.ExperimentalFeatureGate)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
