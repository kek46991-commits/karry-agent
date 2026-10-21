import os
from uuid import uuid4

import pytest

from karry_video.database import Database
from karry_video.repository import Repository, fingerprint
from karry_video.schemas import ProjectFields


@pytest.mark.skipif(not os.getenv("VIDEO_TEST_DATABASE_URL"), reason="VIDEO_TEST_DATABASE_URL未指定：専用テストDB以外には接続しません")
def test_postgresql_crud_and_json():
    database = Database(os.environ["VIDEO_TEST_DATABASE_URL"])
    database.initialize()
    repo = Repository(database)
    project_id = str(uuid4())
    try:
        project = repo.create_project(project_id, ProjectFields(title="PostgreSQL統合", tts={"text": "Hello", "voice": "en"}).model_dump(), f"images/{project_id}.png")
        assert project["tts"]["text"] == "Hello"
        updated = repo.patch_project(project_id, project["version"], {"aspect_ratio": "16:9", "duration_seconds": 4.5})
        assert updated["duration_seconds"] == 4.5
        owner = str(uuid4())
        job, created = repo.reserve_job(project_id, str(uuid4()), "preview", None, fingerprint("preview", "local"), owner, 60)
        assert created
        assert repo.finish_job(job["id"], owner, "succeeded", output_metadata={"has_audio": True}, output_key=f"outputs/{uuid4()}.mp4")
        repo.delete_project(project_id, repo.get_project(project_id)["version"])
    finally:
        database.close()
