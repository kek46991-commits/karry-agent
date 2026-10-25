import json
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .errors import VideoError
from .middleware import auth_error
from .repository import ACTIVE
from .schemas import GenerateRequest, ProjectFields, ProjectPatch, ResumeRequest, ScriptGenerationRequest, TTSParameters
from .script_generation import generate_script_breakdown


class SafeVideoRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except VideoError as error:
                return JSONResponse({"detail": {"code": error.code, "message": error.message}}, status_code=error.status_code, headers={"Cache-Control": "no-store"})
            except (RequestValidationError, ValidationError) as error:
                fields = [{"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()]
                return JSONResponse({"detail": {"code": "validation_error", "message": "入力値を確認してください。", "fields": fields}}, status_code=422)
            except SQLAlchemyError:
                return JSONResponse({"detail": {"code": "database_unavailable", "message": "動画プロジェクトDBに接続できません。"}}, status_code=503)
            except OSError:
                return JSONResponse({"detail": {"code": "storage_unavailable", "message": "動画ストレージを利用できません。"}}, status_code=503)
        return handler


def public_project(project):
    fields = ("title", "aspect_ratio", "duration_seconds", "tts", "render_prompt", "status", "active_job_id", "error_code", "version", "created_at", "updated_at")
    value = {key: project[key] for key in fields}
    value["project_id"] = project["id"]
    value["image_url"] = f"/api/video/projects/{project['id']}/image"
    value["download_url"] = f"/api/video/projects/{project['id']}/download" if project["output_key"] else None
    return value


def public_job(job, job_prefix="/jobs"):
    fields = ("project_id", "request_id", "kind", "provider", "provider_job_id", "status", "phase", "error_code", "output_metadata", "created_at", "updated_at")
    value = {key: job[key] for key in fields}
    value["job_id"] = job["id"]
    value["status_url"] = f"/api/video{job_prefix}/{job['id']}"
    value["download_url"] = f"/api/video{job_prefix}/{job['id']}/download" if job["output_key"] else None
    value["lease_expired"] = job["status"] in ACTIVE and job["lease_until"] <= time.time()
    value["can_resume"] = value["lease_expired"] and not (job["kind"] == "render" and job["phase"] == "submitting" and not job["provider_job_id"])
    return value


def public_script(script):
    return {key: script[key] for key in ("id", "title", "raw_input", "narration_script", "visual_prompt", "scenes", "created_at")}


def create_router(service, auth_dependency=None, *, job_prefix="/jobs"):
    if job_prefix not in {"/jobs", "/project-jobs"}:
        raise ValueError("unsupported job route prefix")

    def job_response(job):
        return public_job(job, job_prefix)
    async def default_auth(request: Request):
        error = auth_error(service.settings, request.headers.get("authorization", ""))
        if error:
            status, code, message = error
            raise VideoError(code, message, status)

    router = APIRouter(prefix="/api/video", tags=["video"], dependencies=[Depends(auth_dependency or default_auth)], route_class=SafeVideoRoute)
    repo = service.repository

    @router.post("/projects", status_code=201)
    async def create_project(
        image: UploadFile = File(...),
        title: str = Form(...),
        aspect_ratio: str = Form("9:16"),
        duration_seconds: float = Form(5),
        tts: str = Form("{}"),
        render_prompt: str = Form("Gentle cinematic camera motion, preserve the original subject."),
    ):
        image_key = None
        try:
            try:
                parameters = TTSParameters.model_validate(json.loads(tts))
            except (ValueError, TypeError):
                raise VideoError("invalid_tts", "ttsには正しいJSON設定を指定してください。", 422) from None
            fields = ProjectFields(title=title, aspect_ratio=aspect_ratio, duration_seconds=duration_seconds, tts=parameters, render_prompt=render_prompt)
            chunks, size = [], 0
            while data := await image.read(65536):
                size += len(data)
                if size > service.settings.max_upload_bytes:
                    raise VideoError("image_too_large", "画像の容量上限を超えています。", 413)
                chunks.append(data)
            project_id = str(uuid4())
            image_key = await service.call(service.storage.save_image, project_id, b"".join(chunks))
            project = await service.call(repo.create_project, project_id, fields.model_dump(), image_key)
            return public_project(project)
        except Exception:
            if image_key:
                await service.call(service.storage.path(image_key).unlink, missing_ok=True)
            raise
        finally:
            await image.close()

    @router.get("/projects")
    async def list_projects(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        result = await service.call(repo.list_projects, limit, offset)
        result["items"] = [public_project(project) for project in result["items"]]
        return result

    @router.get("/projects/{project_id}")
    async def get_project(project_id: UUID):
        return public_project(await service.call(repo.get_project, str(project_id)))

    @router.patch("/projects/{project_id}")
    async def patch_project(project_id: UUID, body: ProjectPatch):
        values = body.model_dump(exclude_unset=True, exclude={"version"})
        return public_project(await service.call(repo.patch_project, str(project_id), body.version, values))

    @router.delete("/projects/{project_id}", status_code=204)
    async def delete_project(project_id: UUID, version: int = Query(..., ge=1)):
        keys = await service.call(repo.delete_project, str(project_id), version)
        for key in keys:
            await service.call(service.storage.path(key).unlink, missing_ok=True)
        return Response(status_code=204)

    @router.get("/projects/{project_id}/status")
    async def project_status(project_id: UUID):
        return public_project(await service.call(repo.get_project, str(project_id)))

    @router.post("/scripts/generate", status_code=201)
    async def generate_script(body: ScriptGenerationRequest):
        draft = await service.call(generate_script_breakdown, body.raw_input, body.scene_count)
        script = await service.call(repo.create_script, draft)
        return public_script(script)

    @router.get("/scripts")
    async def list_scripts(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        result = await service.call(repo.list_scripts, limit, offset)
        result["items"] = [public_script(script) for script in result["items"]]
        return result

    @router.get("/scripts/{script_id}")
    async def get_script(script_id: int):
        return public_script(await service.call(repo.get_script, script_id))

    def protected_file(key, filename, media_type):
        if not key:
            raise VideoError("output_not_ready", "動画はまだ生成されていません。生成ジョブの状態を確認してください。", 409)
        path = service.storage.path(key)
        if not path.is_file():
            raise VideoError("output_missing", "保存された動画・画像が見つかりません。永続ストレージの設定を確認してください。", 404)
        return FileResponse(path, media_type=media_type, filename=filename, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.get("/projects/{project_id}/image")
    async def project_image(project_id: UUID):
        project = await service.call(repo.get_project, str(project_id))
        return protected_file(project["image_key"], f"{project_id}.png", "image/png")

    @router.get("/projects/{project_id}/download")
    async def project_download(project_id: UUID):
        project = await service.call(repo.get_project, str(project_id))
        return protected_file(project["output_key"], f"{project_id}.mp4", "video/mp4")

    @router.post("/generate/preview", status_code=202)
    async def generate_preview(body: GenerateRequest):
        return job_response(await service.generate(body, "preview"))

    @router.post("/generate/render", status_code=202)
    async def generate_render(body: GenerateRequest):
        return job_response(await service.generate(body, "render"))

    @router.get(f"{job_prefix}/{{job_id}}")
    async def get_job(job_id: UUID):
        return job_response(await service.call(repo.get_job, str(job_id)))

    @router.get(f"{job_prefix}/{{job_id}}/download")
    async def job_download(job_id: UUID):
        job = await service.call(repo.get_job, str(job_id))
        return protected_file(job["output_key"], f"{job_id}.mp4", "video/mp4")

    @router.post(f"{job_prefix}/{{job_id}}/resume", status_code=202)
    async def resume_job(job_id: UUID):
        return job_response(await service.resume(str(job_id)))

    @router.post(f"{job_prefix}/{{job_id}}/abandon")
    async def abandon_job(job_id: UUID, body: ResumeRequest):
        return job_response(await service.call(repo.abandon_job, str(job_id), body.acknowledge_unknown))

    return router
