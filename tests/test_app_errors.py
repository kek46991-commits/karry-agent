import pytest
from fastapi import HTTPException

from app import get_agent


def test_missing_gemini_key_is_reported_as_actionable_503(monkeypatch):
    def missing_key_client():
        raise ValueError("GEMINI_API_KEY is not set in environment variables.")

    monkeypatch.setattr("karry_agent.core.get_client", missing_key_client)
    with pytest.raises(HTTPException) as raised:
        get_agent()
    assert raised.value.status_code == 503
    assert "GEMINI_API_KEY" in str(raised.value.detail)
