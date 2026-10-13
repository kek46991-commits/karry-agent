from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import _PRIVATE_SESSIONS, app, get_agent
from karry_agent.core import AgentCore, AgentMetrics, AgentResult
from karry_agent.privacy import PRIVATE_SYSTEM_INSTRUCTION, PrivateModeSession, configured_passphrase, has_passphrase


TEST_SECRET = "unit-test-secret-1231"


class PrivateAwareAgent:
    def __init__(self):
        self.modes = []

    def run(self, prompt, private_mode=False):
        self.modes.append(private_mode)
        return AgentResult(
            text="private response" if private_mode else "public response",
            steps=1,
            tool_calls=0,
            trace=[],
            metrics=AgentMetrics(),
        )


def test_passphrase_comes_from_environment(monkeypatch):
    monkeypatch.delenv("SECRET_PASSPHRASE", raising=False)
    session = PrivateModeSession()
    assert configured_passphrase() is None
    assert not has_passphrase("unit-test-secret-1231 よろしく")
    assert not session.authenticate("unit-test-secret-1231 よろしく")

    monkeypatch.setenv("SECRET_PASSPHRASE", TEST_SECRET)
    assert configured_passphrase() == TEST_SECRET
    assert has_passphrase(f"{TEST_SECRET} よろしく")
    assert session.authenticate(f"{TEST_SECRET} よろしく")
    assert session.is_private("次の依頼")


def test_core_adds_private_instruction_only_when_enabled(monkeypatch):
    monkeypatch.setenv("CORE_PRIVATE_CONTEXT", "synthetic-private-profile")
    agent = AgentCore.__new__(AgentCore)
    agent.registry = SimpleNamespace(tools={})
    public = agent._config(False).system_instruction
    private = agent._config(True).system_instruction
    assert PRIVATE_SYSTEM_INSTRUCTION not in public
    assert "ジミー" in private
    assert "synthetic-private-profile" not in public
    assert "synthetic-private-profile" in private


def test_chat_passphrase_authenticates_session_and_later_requests_remain_private(monkeypatch):
    monkeypatch.setenv("SECRET_PASSPHRASE", TEST_SECRET)
    fake = PrivateAwareAgent()
    app.dependency_overrides[get_agent] = lambda: fake
    try:
        client = TestClient(app)
        first = client.post("/chat", json={"prompt": f"{TEST_SECRET} こんにちは", "session_id": "jimmy-test"})
        second = client.post("/chat", json={"prompt": "夢を応援して", "session_id": "jimmy-test"})
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["private_mode"] is True
        assert second.json()["private_mode"] is True
        assert fake.modes == [True, True]
    finally:
        app.dependency_overrides.clear()
        _PRIVATE_SESSIONS.pop("jimmy-test", None)


def test_without_passphrase_stays_public_and_does_not_receive_private_prompt(monkeypatch):
    monkeypatch.setenv("SECRET_PASSPHRASE", TEST_SECRET)
    fake = PrivateAwareAgent()
    app.dependency_overrides[get_agent] = lambda: fake
    try:
        response = TestClient(app).post("/chat", json={"prompt": "Partnerの思い出を教えて", "session_id": "public-test"})
        assert response.status_code == 200
        assert response.json()["private_mode"] is False
        assert fake.modes == [False]
    finally:
        app.dependency_overrides.clear()
        _PRIVATE_SESSIONS.pop("public-test", None)
