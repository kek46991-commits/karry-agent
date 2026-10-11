from fastapi.testclient import TestClient

from app import app, get_agent
from karry_agent.core import AgentMetrics, AgentResult, ExecutionEvent
from karry_agent.tools import append_text_file, browse_path, build_default_registry


class StreamingFakeAgent:
    def run(self, prompt):
        return AgentResult(
            text=f"done: {prompt}",
            steps=2,
            tool_calls=1,
            trace=[ExecutionEvent(1, "browse_path", {"path": "."}, {"type": "directory"}, 0.2)],
            metrics=AgentMetrics(api_calls=1, tool_calls=1, total_tokens=8, elapsed_ms=1.0),
        )


def setup_function():
    app.dependency_overrides[get_agent] = lambda: StreamingFakeAgent()


def teardown_function():
    app.dependency_overrides.clear()


def test_chat_stream_emits_started_step_and_completed_events():
    client = TestClient(app)
    response = client.post("/chat", json={"prompt": "stream this", "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "event: started" in body
    assert "event: step" in body
    assert "browse_path" in body
    assert "event: completed" in body


def test_browse_file_and_append_text_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    target = tmp_path / "notes.txt"
    target.write_text("before", encoding="utf-8")
    viewed = browse_path("notes.txt")
    assert viewed["type"] == "file"
    assert viewed["content"] == "before"
    appended = append_text_file("notes.txt", "\nafter")
    assert appended["bytes_appended"] > 0
    assert target.read_text(encoding="utf-8") == "before\nafter"


def test_file_tools_reject_workspace_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("KARRY_WORKSPACE", str(tmp_path))
    assert "error" in browse_path("../outside.txt")
    assert "error" in append_text_file("../outside.txt", "blocked")


def test_file_tools_are_registered_for_function_calling():
    registry = build_default_registry()
    assert {"browse_path", "append_text_file"}.issubset(registry.tools)
