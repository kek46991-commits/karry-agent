import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import chat_webtoon
from karry_agent.core import AgentMetrics, AgentResult
from karry_agent.webtoon.api import WebtoonBusy, build_router

AUTH = {"Authorization": "Bearer synthetic-owner"}
STORY = "放課後の図書室。ミナは眼鏡を押し上げた。\n「見つけた！」とミナは叫んだ。"


class EchoAgent:
    def __init__(self):
        self.prompts = []

    def run(self, prompt, **kw):
        self.prompts.append(prompt)
        return AgentResult(text="Answer: " + prompt, steps=1, tool_calls=0, metrics=AgentMetrics())


@pytest.mark.parametrize("text", ["この小説をマンガにして\n" + STORY, "漫画を作って\n本文", "4コマ漫画にしてください",
                                  "ウェブトゥーン化して\n本文", "Turn this story into a webtoon\nOnce upon a time"])
def test_webtoon_requests_are_detected(text):
    assert chat_webtoon.is_webtoon_request(text)


@pytest.mark.parametrize("text", ["マンガの描き方を教えて", "漫画を作ることはできますか", "おすすめの漫画は？", "漫画風の動画を作って",
                                  "こんにちは\nこの小説をマンガにして", "動画を作って", ""])
def test_other_messages_are_left_to_the_agent(text):
    assert not chat_webtoon.is_webtoon_request(text)


def test_panel_count_comes_from_the_instruction_and_is_clamped():
    assert chat_webtoon.requested_panels("この小説を4コマでマンガにして") == 4
    assert chat_webtoon.requested_panels("30コマの漫画にして") == chat_webtoon.MAX_PANELS
    assert chat_webtoon.requested_panels("マンガにして") == chat_webtoon.DEFAULT_PANELS


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES", raising=False)
    monkeypatch.setenv("KARRY_WEBTOON", "1")
    for name in ["_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR", "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS"]:
        monkeypatch.setattr(web, name, {})
    agent, started = EchoAgent(), []

    def start(text, max_panels):
        started.append((text, max_panels))
        if text.startswith("[busy]"):
            raise WebtoonBusy("別のマンガを生成中です。")
        return {"job_id": "d" * 32, "status": "processing"}

    monkeypatch.setattr(web._WEBTOON_ROUTER, "start_job", start)
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    client = TestClient(web.app)
    session_id = client.post("/api/sessions", json={"title": "webtoon"}, headers=AUTH).json()["session_id"]
    yield client, session_id, agent, started
    web.app.dependency_overrides.pop(web.get_agent, None)


def test_chat_prompt_starts_the_webtoon_job_without_the_model(chat):
    client, sid, agent, started = chat
    prompt = "この小説を4コマでマンガにして\n" + STORY
    body = client.post("/api/chat", json={"prompt": prompt, "session_id": sid}, headers=AUTH).json()
    assert started == [(STORY, 4)] and agent.prompts == []
    assert body["status"] == "completed" and chat_webtoon.job_marker("d" * 32) in body["text"]
    assert body["trace"][0]["action"] == chat_webtoon.ACTION and body["trace"][0]["observation"]["job_id"] == "d" * 32
    history = client.get(f"/api/sessions/{sid}", headers=AUTH).json()["messages"]
    assert chat_webtoon.job_marker("d" * 32) in history[-1]["content"]


def test_busy_and_oversized_stories_are_reported_in_chat(chat):
    client, sid, agent, started = chat
    body = client.post("/api/chat", json={"prompt": "マンガにして\n[busy] 本文", "session_id": sid}, headers=AUTH).json()
    assert body["status"] == "failed" and "別のマンガを生成中" in body["text"]
    body = client.post("/api/chat", json={"prompt": "マンガにして\n" + "あ" * 12_001, "session_id": sid}, headers=AUTH).json()
    assert body["status"] == "failed" and "長すぎます" in body["text"] and len(started) == 1 and agent.prompts == []


def test_ordinary_chat_and_disabled_flag_use_the_agent(chat, monkeypatch):
    client, sid, agent, started = chat
    client.post("/api/chat", json={"prompt": "こんにちは", "session_id": sid}, headers=AUTH)
    client.post("/api/chat", json={"prompt": "海辺の動画を作って", "session_id": sid}, headers=AUTH)
    monkeypatch.delenv("KARRY_WEBTOON")
    client.post("/api/chat", json={"prompt": "マンガにして\n" + STORY, "session_id": sid}, headers=AUTH)
    assert started == [] and len(agent.prompts) == 3


def test_router_start_job_runs_the_pipeline_and_refuses_a_second_concurrent_job():
    calls = []

    def pipeline(text, max_panels, progress):
        calls.append((text, max_panels))
        return {"manifest": {"title": "t", "panels": []}, "panels": []}

    done = build_router(lambda: None, runner=lambda fn: fn(), pipeline=pipeline)
    assert done.start_job("本文", 3)["status"] == "succeeded" and calls == [("本文", 3)]
    pending = build_router(lambda: None, runner=lambda fn: None, pipeline=pipeline)
    assert pending.start_job("本文")["status"] == "processing"
    with pytest.raises(WebtoonBusy):
        pending.start_job("別の本文")
