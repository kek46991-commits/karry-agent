from fastapi.testclient import TestClient

from app import app, get_agent
from karry_agent.core import AgentMetrics, AgentResult


class UiAgent:
    def run(self, prompt):
        return AgentResult(text=f"UI response: {prompt}", steps=1, tool_calls=0, metrics=AgentMetrics())


def setup_function():
    app.dependency_overrides[get_agent] = lambda: UiAgent()


def teardown_function():
    app.dependency_overrides.clear()


def test_root_serves_japanese_web_ui():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "karry-agent" in response.text
    assert "日本語 自律型 AI エージェント" in response.text
    assert "stream:true" in response.text


def test_static_asset_is_available():
    response = TestClient(app).get("/static/index.html")
    assert response.status_code == 200
    assert "Thought" in response.text
