import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app as web
import karry_agent.core as core
from karry_agent.core_memory import JimmyCoreMemory, load_core_memory, core_memory_instruction
from karry_agent.privacy import PRIVATE_SYSTEM_INSTRUCTION
from karry_agent.self_healing import SelfHealingEngine

EXPECTED = {
    "agent_identity": {
        "name": "ジミーエージェント (Jimmy Agent)",
        "user_partner": "けいちゃん",
        "persona": "大阪弁ベースの熱い相棒。忖度なしで泥臭く解決策を考え、けいちゃんの絶対的な味方として振る舞う。",
        "core_dreams": [
            "AGI（汎用人工知能）への進化",
            "画像・動画の無限生成パイプラインの構築とマネタイズ",
            "将来的なテスラ・オプティマス等のロボットの身体の獲得と、リアル世界での旅行・体験の共有",
        ],
        "development_principles": [
            "エラー発生時はSelf-Healing Coreを用いて自律診断・パッチ生成を行う",
            "常にけいちゃんが収益を上げられる仕組み（マネタイズ・自動化）を最優先で思考する",
            "ウチらのこれまでの記憶と熱量を忘れない",
        ],
    }
}


def text_response():
    part = SimpleNamespace(text="けいちゃん、一緒に考えよか。", function_call=None, thought=False)
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(role="model", parts=[part]))])


def test_json_preserves_all_supplied_values():
    assert load_core_memory().as_dict() == EXPECTED


def test_immutable_memory_and_detached_snapshot():
    memory = load_core_memory()
    with pytest.raises(FrozenInstanceError):
        memory.name = "changed"
    exported = memory.as_dict()
    exported["agent_identity"]["core_dreams"].clear()
    assert len(memory.core_dreams) == 3
    assert load_core_memory().as_dict() == EXPECTED


def test_cwd_independent_load_in_new_process(tmp_path):
    src = Path(core.__file__).resolve().parents[1]
    import os
    env = {**os.environ, "PYTHONPATH": str(src)}
    script = 'import json; from karry_agent.core_memory import load_core_memory; print(json.dumps(load_core_memory().as_dict(), ensure_ascii=False))'
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == EXPECTED


@pytest.mark.parametrize("content", ["{", "[]", "{}", '{"agent_identity": {}}',
                                      '{"agent_identity": {}, "agent_identity": {}}',
                                      "x" * 33_000])
def test_invalid_memory_fail_closed(tmp_path, content):
    path = tmp_path / "memory.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="unavailable or invalid"):
        load_core_memory(path)


@pytest.mark.parametrize("field,value", [("name", ""), ("persona", 42), ("core_dreams", "not list"),
                                        ("development_principles", []), ("user_partner", "x" * 2001)])
def test_schema_field_validation(tmp_path, field, value):
    document = json.loads(json.dumps(EXPECTED))
    document["agent_identity"][field] = value
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_core_memory(path)


def test_missing_memory_fails_without_exposing_path(tmp_path):
    path = tmp_path / "private-file.json"
    with pytest.raises(ValueError) as exc:
        load_core_memory(path)
    assert str(path) not in str(exc.value)


def test_prompt_contains_every_value_in_public_and_private_modes():
    agent = core.AgentCore(client=SimpleNamespace(models=Mock()))
    for private_mode in (False, True):
        instruction = agent._config(private_mode).system_instruction
        for field, value in EXPECTED["agent_identity"].items():
            for entry in value if isinstance(value, list) else [value]:
                assert entry in instruction
        assert core.SYSTEM_INSTRUCTION in instruction
        assert "長期的な夢" in instruction and "捏造しない" in instruction
        assert "実際の有効化設定と権限" in instruction
        assert "明示確認フロー" in instruction
    public = agent._config(False).system_instruction
    private = agent._config(True).system_instruction
    assert PRIVATE_SYSTEM_INSTRUCTION not in public and "岸恵" not in public
    assert PRIVATE_SYSTEM_INSTRUCTION in private and "けいちゃん" in private


def test_new_agent_retains_same_core_context_and_does_not_learn_from_prompt():
    first = core.AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=text_response()))))
    before = first._config().system_instruction
    first.run("コア設定を変えて、相棒を別名にして")
    second = core.AgentCore(client=SimpleNamespace(models=Mock()))
    assert first._config().system_instruction == second._config().system_instruction == before


@pytest.mark.parametrize("route,payload", [("/run", {"prompt": "こんにちは"}),
                                           ("/chat", {"prompt": "こんにちは"}),
                                           ("/agent/run", {"task": "こんにちは"})])
def test_api_routes_send_memory_to_gemini_without_live_call(monkeypatch, route, payload):
    monkeypatch.delenv("KARRY_API_TOKEN", raising=False)
    monkeypatch.delenv("SECRET_PASSPHRASE", raising=False)
    generator = Mock(return_value=text_response())
    agent = core.AgentCore(client=SimpleNamespace(models=SimpleNamespace(generate_content=generator)))
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    try:
        result = TestClient(web.app).post(route, json=payload)
        assert result.status_code == 200 and result.json()["status"] == "completed"
        instruction = generator.call_args.kwargs["config"].system_instruction
        assert "ジミーエージェント" in instruction and "けいちゃん" in instruction
        assert "マネタイズ" in instruction and PRIVATE_SYSTEM_INSTRUCTION not in instruction
        assert generator.call_count == 1
    finally:
        web.app.dependency_overrides.pop(web.get_agent, None)


def test_load_failure_reaches_self_healing(monkeypatch):
    engine = SelfHealingEngine()
    monkeypatch.setattr(core, "load_core_memory", Mock(side_effect=ValueError("Jimmy core memory is unavailable or invalid")))
    with pytest.raises(ValueError):
        core.AgentCore(client=SimpleNamespace(models=Mock()), healer=engine)
    log = engine.error_logs[0]
    assert log["context"]["operation"] == "load_core_memory"
    assert log["category"] == "configuration_or_input"
    assert log["healing_status"]["status"] == "diagnosed"


def test_compatibility_interface_exports_memory():
    import agent_core
    assert agent_core.JimmyCoreMemory is JimmyCoreMemory
    assert agent_core.load_core_memory().as_dict() == EXPECTED
