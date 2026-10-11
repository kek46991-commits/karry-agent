from fastapi.testclient import TestClient

from app import app, get_agent
from karry_agent.core import AgentMetrics, AgentResult


class FakeAgent:
    def run(self, prompt):
        return AgentResult(
            text=f"received: {prompt}",
            steps=1,
            tool_calls=0,
            metrics=AgentMetrics(api_calls=1, total_tokens=4, elapsed_ms=1.5),
        )


def setup_function():
    app.dependency_overrides[get_agent] = lambda: FakeAgent()


def teardown_function():
    app.dependency_overrides.clear()


def test_health_endpoint():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "karry-agent"}


def test_run_endpoint_returns_agent_result_and_metrics():
    client = TestClient(app)
    response = client.post("/run", json={"prompt": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "received: hello"
    assert body["metrics"]["api_calls"] == 1
    assert body["metrics"]["total_tokens"] == 4


def test_run_endpoint_rejects_empty_prompt():
    client = TestClient(app)
    response = client.post("/run", json={"prompt": ""})
    assert response.status_code == 422
