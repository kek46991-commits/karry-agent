"""Owner-only async API for the webtoon pipeline (experimental, in-memory per process)."""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import publisher
from .pipeline import WebtoonError, run_pipeline

MAX_JOBS = 10
BUSY_MESSAGE = "別のマンガを生成中です。完了後にもう一度お試しください。"


class WebtoonBusy(RuntimeError):
    pass


class WebtoonRequest(BaseModel):
    novel_text: str = Field(..., min_length=1, max_length=12_000)
    max_panels: int = Field(default=6, ge=1, le=12)


def build_router(auth: Callable[..., Any], runner: Callable[[Callable[[], None]], None] | None = None,
                 pipeline: Callable[..., dict[str, Any]] = run_pipeline) -> APIRouter:
    router = APIRouter(prefix="/api/webtoon", tags=["webtoon"], dependencies=[Depends(auth)])
    jobs: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    run = runner or (lambda fn: threading.Thread(target=fn, name="karry-webtoon", daemon=True).start())

    def view(job: dict[str, Any]) -> dict[str, Any]:
        manifest = job.get("manifest") or {}
        return {"job_id": job["id"], "status": job["status"], "progress": job["progress"], "error": job.get("error"),
                "title": manifest.get("title"), "parser": manifest.get("parser"), "characters": manifest.get("characters"),
                "character_sheet_sha256": manifest.get("character_sheet_sha256"),
                "consistent_prompts": manifest.get("consistent_prompts"),
                "panels": [{k: v for k, v in p.items() if k != "prompt"} | {"url": f"/api/webtoon/jobs/{job['id']}/panels/{p['index']}"}
                           for p in manifest.get("panels", [])],
                "archive_url": f"/api/webtoon/jobs/{job['id']}/archive" if job["status"] == "succeeded" else None}

    def work(job: dict[str, Any], body: WebtoonRequest) -> None:
        try:
            result = pipeline(body.novel_text, max_panels=body.max_panels,
                              progress=lambda done, total: job.update(progress={"done": done, "total": total}))
            job.update(status="succeeded", manifest=result["manifest"], images=result["panels"])
        except WebtoonError as exc:
            job.update(status="failed", error=exc.code)
        except Exception:  # noqa: BLE001
            job.update(status="failed", error="webtoon_failed")

    def start_job(novel_text: str, max_panels: int = 6) -> dict[str, Any]:
        body = WebtoonRequest(novel_text=novel_text, max_panels=max_panels)
        with lock:
            if any(j["status"] == "processing" for j in jobs.values()):
                raise WebtoonBusy(BUSY_MESSAGE)
            while len(jobs) >= MAX_JOBS:
                jobs.pop(next(iter(jobs)))
            job = {"id": uuid.uuid4().hex, "status": "processing", "progress": {"done": 0, "total": None}, "created_at": time.time()}
            jobs[job["id"]] = job
        run(lambda: work(job, body))
        return view(job)

    @router.post("/jobs", status_code=202)
    def create(body: WebtoonRequest) -> dict[str, Any]:
        try:
            return start_job(body.novel_text, body.max_panels)
        except WebtoonBusy as exc:
            raise HTTPException(409, str(exc)) from None

    def get(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "webtoon job not found")
        return job

    @router.get("/jobs/{job_id}")
    def status(job_id: str) -> dict[str, Any]:
        return view(get(job_id))

    @router.get("/jobs/{job_id}/panels/{index}")
    def panel(job_id: str, index: int) -> Response:
        images = get(job_id).get("images") or []
        if not 1 <= index <= len(images):
            raise HTTPException(404, "panel not found")
        return Response(images[index - 1], media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @router.get("/jobs/{job_id}/archive")
    def archive(job_id: str) -> Response:
        job = get(job_id)
        if job["status"] != "succeeded":
            raise HTTPException(409, "webtoon is not ready")
        return Response(publisher.archive(job["images"], job["manifest"]), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="webtoon-{job_id[:8]}.zip"', "Cache-Control": "private, no-store"})

    router.start_job = start_job
    return router
