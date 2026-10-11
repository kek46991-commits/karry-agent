from fastapi.testclient import TestClient

from app import _PRIVATE_SESSIONS, app, get_agent
from karry_agent.core import AgentMetrics, AgentResult


class FakeAgent:
    def __init__(self):
        self.prompts = []
        self.modes = []

    def run(self, prompt, private_mode=False):
        self.prompts.append(prompt)
        self.modes.append(private_mode)
        return AgentResult(
            text=f"received: {prompt}",
            steps=1,
            tool_calls=0,
            metrics=AgentMetrics(api_calls=1, total_tokens=4, elapsed_ms=1.5),
        )


def test_agent_run_compatibility_endpoint_maps_task_to_existing_core(monkeypatch):
    monkeypatch.delenv("SECRET_PASSPHRASE", raising=False)
    fake = FakeAgent()
    app.dependency_overrides[get_agent] = lambda: fake
    try:
        response = TestClient(app).post("/agent/run", json={"task": "calendar planning"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["task"] == "calendar planning"
        assert body["thought_process"] == "received: calendar planning"
        assert body["next_action"] == "実行完了または次のステップへ移行"
        assert body["private_mode"] is False
        assert body["metrics"]["api_calls"] == 1
        assert fake.prompts == ["calendar planning"]
    finally:
        app.dependency_overrides.clear()


def test_agent_run_compatibility_preserves_private_session_boundary(monkeypatch):
    monkeypatch.setenv("SECRET_PASSPHRASE", "test-secret")
    fake = FakeAgent()
    app.dependency_overrides[get_agent] = lambda: fake
    try:
        client = TestClient(app)
        first = client.post("/agent/run", json={"task": "test-secret hello", "session_id": "compat-jimmy"})
        second = client.post("/agent/run", json={"task": "continue", "session_id": "compat-jimmy"})
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["private_mode"] is True
        assert second.json()["private_mode"] is True
        assert fake.modes == [True, True]
    finally:
        app.dependency_overrides.clear()
        _PRIVATE_SESSIONS.pop("compat-jimmy", None)


def test_agent_run_compatibility_rejects_empty_task():
    response = TestClient(app).post("/agent/run", json={"task": ""})
    assert response.status_code == 422
