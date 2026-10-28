import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import chat_video
from karry_agent import video_suite as vs
from karry_agent.core import AgentMetrics, AgentResult

AUTH = {"Authorization": "Bearer synthetic-owner"}


class EchoAgent:
    def __init__(self):
        self.prompts = []

    def run(self, prompt, **kw):
        self.prompts.append(prompt)
        return AgentResult(text="Answer: " + prompt, steps=1, tool_calls=0, metrics=AgentMetrics())


@pytest.mark.parametrize("text", ["夕焼けの海辺を歩く少女の動画を作って", "猫が踊るムービーを生成してください",
                                  "この写真で映像を作成して", "Make a video of a rainy Tokyo street", "動画にして"])
def test_video_requests_are_detected(text):
    assert chat_video.is_video_request(text)


@pytest.mark.parametrize("text", ["動画の作り方を教えて", "動画を作ることはできますか", "How to make a video?",
                                  "昨日見た動画が面白かった", "ホームページを作って", ""])
def test_non_requests_are_left_to_the_agent(text):
    assert not chat_video.is_video_request(text)


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in ["_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR", "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS"]:
        monkeypatch.setattr(web, name, {})
    agent, started = EchoAgent(), []

    def start(session_id, text, image, request_id):
        started.append((session_id, text, image, request_id))
        if text.startswith("[busy]"):
            raise vs.VideoError("このタスクでは別の自動動画を生成中です。", 409, "autopilot_busy")
        return {"id": "f" * 32}, 202

    monkeypatch.setattr(web._VIDEO_ROUTER, "start_autopilot", start)
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    session_id = client.post("/api/sessions", json={"title": "video"}, headers=AUTH).json()["session_id"]
    yield client, session_id, agent, started
    web.app.dependency_overrides.pop(web.get_agent, None)


def test_chat_prompt_starts_autopilot_without_calling_the_model(chat):
    client, sid, agent, started = chat
    body = client.post("/api/chat", json={"prompt": "海辺の動画を作って", "session_id": sid, "request_id": "r-1"}, headers=AUTH).json()
    assert started == [(sid, "海辺の動画を作って", None, "r-1")] and agent.prompts == []
    assert body["status"] == "completed" and chat_video.job_marker("f" * 32) in body["text"]
    assert body["trace"][0]["action"] == chat_video.ACTION and body["trace"][0]["observation"]["job_id"] == "f" * 32
    history = client.get(f"/api/sessions/{sid}", headers=AUTH).json()["messages"]
    assert chat_video.job_marker("f" * 32) in history[-1]["content"]


def test_video_errors_are_reported_in_chat(chat):
    client, sid, agent, _ = chat
    body = client.post("/api/chat", json={"prompt": "[busy] 動画を作って", "session_id": sid}, headers=AUTH).json()
    assert body["status"] == "failed" and "別の自動動画を生成中" in body["text"] and agent.prompts == []
    assert body["trace"][0]["observation"]["code"] == "autopilot_busy"


def test_ordinary_chat_and_portfolio_mode_use_the_agent(chat, monkeypatch):
    client, sid, agent, started = chat
    client.post("/api/chat", json={"prompt": "こんにちは", "session_id": sid}, headers=AUTH)
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES")
    client.post("/api/chat", json={"prompt": "海辺の動画を作って", "session_id": sid}, headers=AUTH)
    assert started == [] and len(agent.prompts) == 2


def test_attached_image_is_passed_to_autopilot():
    seen = []
    result = chat_video.run(lambda *args: seen.append(args) or ({"id": "e" * 32}, 202), "s", "この画像で動画を作って",
                            [{"data": b"png-bytes", "mime_type": "image/png"}])
    assert seen[0][2] == b"png-bytes" and result.trace[0].arguments["with_image"] is True


def test_video_request_does_not_need_the_model_but_private_fallback_still_builds_it(chat, monkeypatch):
    client, sid, _, started = chat
    built = []

    def factory():
        built.append(1)
        raise ValueError("GEMINI_API_KEY is not set in environment variables.")

    web.app.dependency_overrides[web.get_agent] = factory
    body = client.post("/api/chat", json={"prompt": "海辺の動画を作って", "session_id": sid}, headers=AUTH).json()
    assert body["status"] == "completed" and started and built == []
    monkeypatch.setattr(web, "_private_mode", lambda *_: True)
    assert client.post("/api/chat", json={"prompt": "海辺の動画を作って", "session_id": sid}, headers=AUTH).status_code >= 400
    assert built == [1] and len(started) == 1
