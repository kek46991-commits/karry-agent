from types import SimpleNamespace

import pytest

from karry_video.errors import VideoError
from karry_video.schemas import ScriptDraft, ScriptScene
from karry_video.script_generation import generate_script_breakdown


def _draft(scene_count=2):
    return ScriptDraft(
        title="雨の駅",
        visual_prompt="雨の夜の小さな駅、青い傘、静かなシネマティックな色調。",
        scenes=[
            ScriptScene(scene_number=8, duration_seconds=5, narration="少女は傘を見つけた。", visual_prompt="雨のホームに青い傘。"),
            ScriptScene(scene_number=7, duration_seconds=4, narration="遠くで汽笛が鳴る。", visual_prompt="少女が霧の向こうを見る。"),
        ][:scene_count],
    )


def test_generator_uses_gemini_structured_output_and_normalizes_scene_order(monkeypatch):
    from google import genai

    calls = []
    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(parsed=_draft())

    fake_client = SimpleNamespace(models=FakeModels())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("KARRY_SCRIPT_MODEL", "test-model")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: fake_client)

    result = generate_script_breakdown("雨の駅で少女が青い傘を拾う。", 2)

    assert result["title"] == "雨の駅"
    assert result["raw_input"] == "雨の駅で少女が青い傘を拾う。"
    assert [scene["scene_number"] for scene in result["scenes"]] == [1, 2]
    assert result["narration_script"] == "少女は傘を見つけた。\n遠くで汽笛が鳴る。"
    assert calls[0]["model"] == "test-model"
    assert calls[0]["config"].response_mime_type == "application/json"
    assert calls[0]["config"].response_schema is ScriptDraft
    assert "必ず2個" in calls[0]["contents"]


def test_generator_rejects_wrong_scene_count_without_saving(monkeypatch):
    from google import genai

    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: SimpleNamespace(parsed=_draft(scene_count=1))))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: fake_client)

    with pytest.raises(VideoError) as error:
        generate_script_breakdown("雨の駅", 2)
    assert error.value.code == "script_generation_invalid_output"
    assert error.value.status_code == 502
