import pytest


@pytest.fixture(autouse=True)
def explicit_test_storage_mode(monkeypatch):
    # No production bucket or model is contacted by the test suite.
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
    # Existing tests must not initialize a real video database at app startup.
    monkeypatch.setenv('KARRY_VIDEO_PROJECTS_ENABLED', '0')
