import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from karry_video.database import Database, normalize_database_url
from karry_video.errors import VideoError
from karry_video.repository import Repository, fingerprint
from karry_video.schemas import ProjectFields


@pytest.fixture
def repo(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'projects.sqlite'}")
    database.initialize()
    yield Repository(database)
    database.close()


def create(repo):
    identifier = str(uuid4())
    return repo.create_project(identifier, ProjectFields(title="テスト").model_dump(), f"images/{identifier}.png")


def reserve(repo, project, request_id=None):
    return repo.reserve_job(project["id"], request_id or str(uuid4()), "preview", None, fingerprint("preview", "local"), str(uuid4()), 60)


def test_crud_and_reopen(repo):
    project = create(repo)
    assert project["status"] == "draft"
    assert project["version"] == 1
    assert repo.list_projects()["total"] == 1
    assert repo.get_project(project["id"])["tts"]["voice"] == "en"
    updated = repo.patch_project(project["id"], 1, {"title": "改訂", "aspect_ratio": "16:9", "duration_seconds": 7.5})
    assert updated["version"] == 2
    with pytest.raises(VideoError) as error:
        repo.patch_project(project["id"], 1, {"title": "旧version"})
    assert error.value.status_code == 409
    repo.delete_project(project["id"], 2)
    assert repo.list_projects()["total"] == 0
    with pytest.raises(VideoError) as error:
        repo.get_project(project["id"])
    assert error.value.status_code == 404


def test_request_idempotency_and_active_lock(repo):
    project = create(repo)
    request_id = str(uuid4())
    job, created = reserve(repo, project, request_id)
    assert created
    duplicate, created = reserve(repo, project, request_id)
    assert duplicate["id"] == job["id"] and not created
    with pytest.raises(VideoError):
        reserve(repo, project)
    with pytest.raises(VideoError):
        repo.delete_project(project["id"], repo.get_project(project["id"])["version"])
    with pytest.raises(VideoError):
        repo.patch_project(project["id"], repo.get_project(project["id"])["version"], {"title": "blocked"})
    with pytest.raises(VideoError):
        repo.find_request(project["id"], request_id, fingerprint("render", "auto"))
    assert repo.finish_job(job["id"], job["owner_token"], "succeeded", output_key="outputs/test.mp4", output_metadata={"duration_seconds": 5})
    assert repo.get_project(project["id"])["active_job_id"] is None
    assert reserve(repo, repo.get_project(project["id"]))[1]


def test_two_connections_one_project_claim(repo):
    project = create(repo)
    def attempt(_):
        try:
            return reserve(repo, project)[1]
        except VideoError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count(True) == 1


def test_resume_lease_and_unknown_submission(repo):
    project = create(repo)
    owner = str(uuid4())
    job, _ = repo.reserve_job(project["id"], str(uuid4()), "render", "replicate", fingerprint("render", "auto"), owner, 60)
    with pytest.raises(VideoError):
        repo.claim_resume(job["id"], str(uuid4()), 60)
    assert repo.update_job(job["id"], owner, phase="submitting")
    assert repo.finish_job(job["id"], owner, "awaiting_resume", error_code="submission_unknown", release=False)
    with pytest.raises(VideoError) as error:
        repo.claim_resume(job["id"], str(uuid4()), 60)
    assert error.value.code == "submission_unknown"
    assert repo.reconcile_provider_id(job["id"], "replicate", "safe-receipt-id")
    resumed_owner = str(uuid4())
    resumed = repo.claim_resume(job["id"], resumed_owner, 60)
    assert resumed["provider_job_id"] == "safe-receipt-id"
    assert not repo.finish_job(job["id"], owner, "failed")
    assert repo.finish_job(job["id"], resumed_owner, "succeeded")


def test_abandon_requires_explicit_acknowledgement(repo):
    project = create(repo)
    job, _ = reserve(repo, project)
    repo.finish_job(job["id"], job["owner_token"], "interrupted", release=False)
    with pytest.raises(VideoError):
        repo.abandon_job(job["id"], False)
    assert repo.abandon_job(job["id"], True)["status"] == "abandoned"
    assert repo.get_project(project["id"])["active_job_id"] is None


@pytest.mark.parametrize("input_url", ["postgres://user:pass@localhost/test", "postgresql://user:pass@localhost/test", "postgresql+psycopg://user:pass@localhost/test"])
def test_postgres_driver_normalization(input_url):
    assert normalize_database_url(input_url).startswith("postgresql+psycopg://")


def test_unsupported_driver_is_rejected():
    with pytest.raises(ValueError):
        Database("mysql+pymysql://localhost/test")


def test_same_request_two_connections_returns_same_job(repo):
    import threading
    from sqlalchemy import event
    project = create(repo)
    request_id = str(uuid4())
    barrier = threading.Barrier(2)
    local = threading.local()
    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "video_jobs" in statement and "request_id" in statement and not getattr(local, "seen", False):
            local.seen = True
            barrier.wait(timeout=5)
    event.listen(repo.database.engine, "after_cursor_execute", synchronize)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, repo, project, request_id) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        assert results[0][0]["id"] == results[1][0]["id"]
        assert sum(created for _, created in results) == 1
    finally:
        event.remove(repo.database.engine, "after_cursor_execute", synchronize)
