import pytest


@pytest.fixture(autouse=True)
def explicit_test_storage_mode(monkeypatch):
    # No production bucket or model is contacted by the test suite.
    monkeypatch.setenv('KARRY_TASK_STORAGE', 'memory')
