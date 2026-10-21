import hashlib
import time
from datetime import timezone
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .errors import VideoError
from .models import VideoJob, VideoProject, utcnow

ACTIVE = {"queued", "running", "awaiting_resume", "interrupted"}


def _serialize(row):
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if hasattr(value, "isoformat"):
            value = value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
        result[column.name] = value
    return result


def fingerprint(kind, requested_provider):
    return hashlib.sha256(f"{kind}:{requested_provider}".encode()).hexdigest()


class Repository:
    def __init__(self, database):
        self.database = database

    def create_project(self, project_id, fields, image_key):
        with self.database.sessions.begin() as session:
            row = VideoProject(id=project_id, image_key=image_key, **fields)
            session.add(row)
            session.flush()
            return _serialize(row)

    def get_project(self, project_id):
        with self.database.sessions() as session:
            row = session.get(VideoProject, str(project_id))
            if row is None:
                raise VideoError("project_not_found", "指定された動画プロジェクトが見つかりません。", 404)
            return _serialize(row)

    def list_projects(self, limit=20, offset=0):
        with self.database.sessions() as session:
            count = session.scalar(select(func.count()).select_from(VideoProject))
            rows = session.scalars(select(VideoProject).order_by(VideoProject.created_at.desc(), VideoProject.id).offset(offset).limit(limit)).all()
            return {"items": [_serialize(row) for row in rows], "total": count, "limit": limit, "offset": offset}

    def patch_project(self, project_id, version, values):
        with self.database.sessions.begin() as session:
            row = session.get(VideoProject, project_id)
            if row is None:
                raise VideoError("project_not_found", "指定された動画プロジェクトが見つかりません。", 404)
            result = session.execute(update(VideoProject).where(VideoProject.id == project_id, VideoProject.version == version, VideoProject.active_job_id.is_(None)).values(**values, version=VideoProject.version + 1, status="draft", output_key=None, error_code=None, updated_at=utcnow()))
            if result.rowcount != 1:
                raise VideoError("project_conflict", "生成中または更新競合です。最新のversionを確認してください。", 409)
            session.expire_all()
            return _serialize(session.get(VideoProject, project_id))

    def delete_project(self, project_id, version):
        with self.database.sessions.begin() as session:
            row = session.get(VideoProject, project_id)
            if row is None:
                raise VideoError("project_not_found", "指定された動画プロジェクトが見つかりません。", 404)
            keys = [row.image_key]
            keys += [key for key in session.scalars(select(VideoJob.output_key).where(VideoJob.project_id == project_id)).all() if key]
            keys += [f"receipts/{identifier}.json" for identifier in session.scalars(select(VideoJob.id).where(VideoJob.project_id == project_id)).all()]
            result = session.execute(delete(VideoProject).where(VideoProject.id == project_id, VideoProject.version == version, VideoProject.active_job_id.is_(None)))
            if result.rowcount != 1:
                raise VideoError("project_conflict", "生成中または更新競合のため削除できません。", 409)
            return keys

    def find_request(self, project_id, request_id, expected_fingerprint):
        with self.database.sessions() as session:
            row = session.scalar(select(VideoJob).where(VideoJob.project_id == project_id, VideoJob.request_id == request_id))
            if row is None:
                return None
            if row.fingerprint != expected_fingerprint:
                raise VideoError("request_conflict", "同じrequest_idに異なる生成リクエストを指定できません。", 409)
            return _serialize(row)

    def reserve_job(self, project_id, request_id, kind, provider, expected_fingerprint, owner_token, lease_seconds):
        try:
            with self.database.sessions.begin() as session:
                existing = session.scalar(select(VideoJob).where(VideoJob.project_id == project_id, VideoJob.request_id == request_id))
                if existing:
                    if existing.fingerprint != expected_fingerprint:
                        raise VideoError("request_conflict", "request_idが競合しています。", 409)
                    return _serialize(existing), False
                project = session.get(VideoProject, project_id)
                if project is None:
                    raise VideoError("project_not_found", "指定された動画プロジェクトが見つかりません。", 404)
                job_id = str(uuid4())
                snapshot = {key: getattr(project, key) for key in ("id", "image_key", "aspect_ratio", "duration_seconds", "tts", "render_prompt")}
                result = session.execute(update(VideoProject).where(VideoProject.id == project_id, VideoProject.active_job_id.is_(None), VideoProject.version == project.version).values(active_job_id=job_id, status="queued", error_code=None, version=VideoProject.version + 1, updated_at=utcnow()))
                if result.rowcount != 1:
                    existing = session.scalar(select(VideoJob).where(VideoJob.project_id == project_id, VideoJob.request_id == request_id))
                    if existing:
                        if existing.fingerprint != expected_fingerprint:
                            raise VideoError("request_conflict", "request_idが競合しています。", 409)
                        return _serialize(existing), False
                    raise VideoError("project_busy", "このプロジェクトには実行中または確認待ちの生成ジョブがあります。", 409)
                row = VideoJob(id=job_id, project_id=project_id, request_id=request_id, fingerprint=expected_fingerprint, kind=kind, provider=provider, snapshot=snapshot, owner_token=owner_token, lease_until=time.time() + lease_seconds)
                session.add(row)
                session.flush()
                return _serialize(row), True
        except IntegrityError:
            existing = self.find_request(project_id, request_id, expected_fingerprint)
            if existing:
                return existing, False
            raise VideoError("project_conflict", "同時更新のため生成を開始できませんでした。", 409) from None

    def get_job(self, job_id):
        with self.database.sessions() as session:
            row = session.get(VideoJob, str(job_id))
            if row is None:
                raise VideoError("job_not_found", "指定された動画生成ジョブが見つかりません。", 404)
            return _serialize(row)

    def update_job(self, job_id, owner_token, **values):
        with self.database.sessions.begin() as session:
            result = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.owner_token == owner_token).values(**values, updated_at=utcnow()))
            return result.rowcount == 1

    def heartbeat(self, job_id, owner_token, lease_seconds):
        return self.update_job(job_id, owner_token, lease_until=time.time() + lease_seconds)

    def finish_job(self, job_id, owner_token, status, error_code=None, output_key=None, output_metadata=None, release=True):
        with self.database.sessions.begin() as session:
            job = session.get(VideoJob, job_id)
            if job is None or job.owner_token != owner_token:
                return False
            result = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.owner_token == owner_token).values(status=status, error_code=error_code, output_key=output_key, output_metadata=output_metadata, updated_at=utcnow(), lease_until=0, owner_token=None))
            if result.rowcount != 1:
                return False
            values = {"status": status, "error_code": error_code, "version": VideoProject.version + 1, "updated_at": utcnow()}
            if release:
                values["active_job_id"] = None
            if output_key:
                values["output_key"] = output_key
            session.execute(update(VideoProject).where(VideoProject.id == job.project_id, VideoProject.active_job_id == job_id).values(**values))
            return True

    def claim_resume(self, job_id, owner_token, lease_seconds):
        with self.database.sessions.begin() as session:
            row = session.get(VideoJob, job_id)
            if row is None:
                raise VideoError("job_not_found", "ジョブが見つかりません。", 404)
            if row.status not in ACTIVE or row.lease_until > time.time():
                raise VideoError("job_not_resumable", "このジョブは完了済み、または実行プロセスが稼働中です。", 409)
            if row.kind == "render" and row.phase == "submitting" and not row.provider_job_id:
                raise VideoError("submission_unknown", "送信結果が不明です。プロバイダー側を確認し、再送せずに状態を照合してください。", 409)
            result = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.status.in_(ACTIVE), VideoJob.lease_until <= time.time()).values(owner_token=owner_token, lease_until=time.time() + lease_seconds, status="queued", error_code=None, updated_at=utcnow()))
            if result.rowcount != 1:
                raise VideoError("job_conflict", "別のプロセスがジョブを取得しました。", 409)
            session.expire_all()
            return _serialize(session.get(VideoJob, job_id))

    def abandon_job(self, job_id, acknowledge_unknown):
        if not acknowledge_unknown:
            raise VideoError("acknowledgement_required", "外部ジョブは停止されません。重複課金リスクを確認しacknowledge_unknown=trueを指定してください。", 422)
        with self.database.sessions.begin() as session:
            job = session.get(VideoJob, job_id)
            if job is None:
                raise VideoError("job_not_found", "ジョブが見つかりません。", 404)
            result = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.status.in_(ACTIVE), VideoJob.lease_until <= time.time()).values(status="abandoned", owner_token=None, lease_until=0, updated_at=utcnow()))
            if result.rowcount != 1:
                raise VideoError("job_not_abandonable", "実行中または完了済みのジョブは解放できません。", 409)
            session.execute(update(VideoProject).where(VideoProject.id == job.project_id, VideoProject.active_job_id == job_id).values(active_job_id=None, status="draft", error_code=None, version=VideoProject.version + 1, updated_at=utcnow()))
            session.expire_all()
            return _serialize(session.get(VideoJob, job_id))

    def reconcile_provider_id(self, job_id, provider, provider_job_id):
        with self.database.sessions.begin() as session:
            result = session.execute(update(VideoJob).where(VideoJob.id == job_id, VideoJob.provider == provider, VideoJob.provider_job_id.is_(None), VideoJob.phase == "submitting", VideoJob.status.in_(ACTIVE), VideoJob.lease_until <= time.time()).values(provider_job_id=provider_job_id, phase="polling", updated_at=utcnow()))
            return result.rowcount == 1
