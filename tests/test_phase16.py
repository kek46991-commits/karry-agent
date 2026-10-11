from fastapi.testclient import TestClient

from app import app, get_agent
from karry_agent.core import AgentMetrics, AgentResult, ExecutionEvent
from karry_agent.tools import list_workspace


class TraceAgent:
    def run(self, prompt):
        return AgentResult(
            text=f"answer: {prompt}",
            steps=2,
            tool_calls=1,
            trace=[ExecutionEvent(1, "list_workspace", {}, {"entries": []}, 0.5)],
            metrics=AgentMetrics(api_calls=2, tool_calls=1, total_tokens=9, elapsed_ms=2.0),
        )


def setup_function():
    app.dependency_overrides[get_agent] = lambda: TraceAgent()


def teardown_function():
    app.dependency_overrides.clear()


def test_chat_creates_session_and_run_trace_is_retrievable():
    client = TestClient(app)
    response = client.post("/chat", json={"prompt": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"]
    assert body["status"] == "completed"
    assert body["trace"][0]["action"] == "list_workspace"

    run = client.get(f"/runs/{body['run_id']}")
    assert run.status_code == 200
    assert run.json()["metrics"]["total_tokens"] == 9

    session = client.get(f"/sessions/{body['session_id']}")
    assert session.status_code == 200
    assert session.json()["history"][-1]["content"] == "answer: hello"


def test_chat_reuses_session_context():
    client = TestClient(app)
    first = client.post("/chat", json={"prompt": "first", "session_id": "phase16-session"})
    second = client.post("/chat", json={"prompt": "second", "session_id": "phase16-session"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert "user: first" in second.json()["text"]


def test_missing_run_and_session_return_404():
    client = TestClient(app)
    assert client.get("/runs/missing").status_code == 404
    assert client.get("/sessions/missing").status_code == 404


def test_list_workspace_is_safe_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
    (tmp_path / ".secret").write_text("do not expose", encoding="utf-8")
    result = list_workspace(".", max_entries=10)
    paths = [entry["path"] for entry in result["entries"]]
    assert "visible.txt" in paths
    assert ".secret" not in paths


def test_list_workspace_rejects_outside_path(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    result = list_workspace("../")
    assert "error" in result or result is not None
