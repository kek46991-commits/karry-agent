import json
from types import SimpleNamespace

import pytest

from karry_video.errors import VideoError
from karry_video.schemas import ScriptDraft, ScriptScene
from karry_video.script_generation import (
    fallback_script_breakdown,
    generate_script_breakdown,
    generate_script_from_image,
    script_response_schema,
)


def _draft(scene_count=2):
    return ScriptDraft(
        title="雨の駅",
        visual_prompt="A small station on a rainy night, a vivid blue umbrella, cinematic lighting.",
        scenes=[
            ScriptScene(scene_number=8, duration_seconds=8, narration="少女は傘を見つけた。", visual_prompt="A blue umbrella lies on a rain-soaked platform."),
            ScriptScene(scene_number=7, duration_seconds=7, narration="遠くで汽笛が鳴る。", visual_prompt="The girl looks into the fog as a train whistle echoes."),
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
    assert calls[0]["config"].response_schema == script_response_schema(2)
    assert "additionalProperties" not in json.dumps(calls[0]["config"].response_schema)
    assert "必ず2個" in calls[0]["contents"]
    assert "15〜30秒" in calls[0]["contents"]
    assert "英語visual_prompt" in calls[0]["contents"]
    assert sum(scene["duration_seconds"] for scene in result["scenes"]) == 15
    assert result["visual_prompt"].startswith("A small station")


def test_generator_rejects_wrong_scene_count_without_saving(monkeypatch):
    from google import genai

    short_draft = ScriptDraft(title="雨の駅", visual_prompt="A rainy station.", scenes=[ScriptScene(scene_number=1, duration_seconds=15, narration="雨の駅。", visual_prompt="A rainy station platform.")])
    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: SimpleNamespace(parsed=short_draft)))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: fake_client)

    with pytest.raises(VideoError) as error:
        generate_script_breakdown("雨の駅", 2)
    assert error.value.code == "script_generation_invalid_output"
    assert error.value.status_code == 502


@pytest.mark.parametrize("durations", [[9, 5], [16, 15]])
def test_script_draft_requires_total_duration_between_15_and_30_seconds(durations):
    from pydantic import ValidationError

    scenes = [
        ScriptScene(scene_number=index, duration_seconds=seconds, narration="短いナレーション。", visual_prompt="A cinematic animated scene.")
        for index, seconds in enumerate(durations, start=1)
    ]
    with pytest.raises(ValidationError, match="between 15 and 30 seconds"):
        ScriptDraft(title="短編アニメ", visual_prompt="A short cinematic animation.", scenes=scenes)


def test_generator_rescales_short_runtime_and_ignores_extra_keys(monkeypatch):
    from google import genai

    payload = {"title": "雨の駅", "visual_prompt": "A rainy station.", "extra": "ignored", "scenes": [
        {"scene_number": 1, "duration_seconds": 4, "narration": "少女は立っていた。", "visual_prompt": "A girl stands at a station.", "note": "x"},
        {"scene_number": 2, "duration_seconds": 5, "narration": "汽笛が鳴った。", "visual_prompt": "A train whistle echoes."},
    ]}
    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: SimpleNamespace(parsed=None, text="```json\n" + json.dumps(payload) + "\n```")))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: fake_client)

    result = generate_script_breakdown("雨の駅", 2)
    assert [s["duration_seconds"] for s in result["scenes"]] == [7, 8]
    assert result["narration_script"] == "少女は立っていた。\n汽笛が鳴った。"


def test_schema_is_accepted_by_gemini_wire_format():
    from google.genai import types

    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=script_response_schema(3))
    dumped = str(config.model_dump(exclude_none=True))
    assert "additional_properties" not in dumped and "additionalProperties" not in dumped


def _api_error(code, status, message="boom"):
    from google.genai import errors

    return errors.ClientError(code, {"error": {"code": code, "status": status, "message": message}})


def test_generator_retries_then_uses_backup_model_when_primary_is_rejected(monkeypatch):
    from google import genai

    models = []
    def generate_content(**kwargs):
        models.append(kwargs["model"])
        if kwargs["model"] == "primary":
            raise _api_error(404, "NOT_FOUND")
        return SimpleNamespace(parsed=_draft())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("KARRY_SCRIPT_MODEL", "primary")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    result = generate_script_breakdown("雨の駅", 2)
    assert models == ["primary", "gemini-2.5-flash"] and result["title"] == "雨の駅"


def test_generator_retries_invalid_output_once_per_model(monkeypatch):
    from google import genai

    calls = []
    def generate_content(**kwargs):
        calls.append(kwargs["model"])
        return SimpleNamespace(parsed=None, text="not json") if len(calls) == 1 else SimpleNamespace(parsed=_draft())
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    assert generate_script_breakdown("雨の駅", 2)["title"] == "雨の駅" and len(calls) == 2


@pytest.mark.parametrize(("error", "code", "status"), [
    (lambda: _api_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key. API_KEY_INVALID"), "script_ai_auth_failed", 502),
    (lambda: _api_error(403, "PERMISSION_DENIED"), "script_ai_auth_failed", 502),
    (lambda: _api_error(429, "RESOURCE_EXHAUSTED"), "script_ai_rate_limited", 429),
])
def test_generator_reports_key_and_quota_errors_with_specific_codes(monkeypatch, error, code, status):
    from google import genai

    def generate_content(**kwargs):
        raise error()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    with pytest.raises(VideoError) as exc:
        generate_script_breakdown("雨の駅", 2)
    assert exc.value.code == code and exc.value.status_code == status


def test_image_generator_uses_sanitized_image_and_structured_gemini_output(monkeypatch):
    import io
    from PIL import Image
    from google import genai

    image_bytes = io.BytesIO()
    Image.new("RGB", (48, 32), "navy").save(image_bytes, "PNG")
    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(parsed=_draft())

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", lambda **kwargs: SimpleNamespace(models=FakeModels()))
    result = generate_script_from_image(image_bytes.getvalue(), scene_count=2)

    assert result["raw_input"] == "画像入力" and len(result["scenes"]) == 2
    assert calls[0]["config"].response_schema["properties"]["scenes"]["maxItems"] == 2
    assert "画像" in calls[0]["contents"][0] and "必ず2個" in calls[0]["contents"][0]
    part = calls[0]["contents"][1]
    assert part.inline_data.mime_type == "image/jpeg" and part.inline_data.data


def test_image_generator_rejects_invalid_or_animated_image_before_model_call(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with pytest.raises(VideoError) as error:
        generate_script_from_image(b"not an image", scene_count=3)
    assert error.value.code == "invalid_image" and error.value.status_code == 422


def test_fallback_script_is_bounded_valid_and_uses_source_as_narration():
    result = fallback_script_breakdown("夕焼けの海辺を歩き、未来について語り始めた。", 3)
    assert result["fallback_used"] is True
    assert result["narration_script"].replace("\n", "") == "夕焼けの海辺を歩き、未来について語り始めた。"
    assert len(result["scenes"]) == 3 and sum(scene["duration_seconds"] for scene in result["scenes"]) == 15
    assert all(scene["narration"] and scene["visual_prompt"].isascii() for scene in result["scenes"])


def test_fallback_script_for_image_only_has_default_narration_and_valid_duration():
    result = fallback_script_breakdown("", 3, image_input=True)
    assert result["raw_input"] == "画像入力" and len(result["scenes"]) == 3
    assert result["narration_script"] and sum(scene["duration_seconds"] for scene in result["scenes"]) == 15
