import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as web
from karry_agent.webtoon import bubble_renderer, publisher, run_pipeline
from karry_agent.webtoon.api import build_router
from karry_agent.webtoon.character_manager import CharacterManager
from karry_agent.webtoon.script_parser import fallback_parse, normalize, parse_novel

STORY = """放課後の図書室。黒髪で眼鏡をかけたミナは、窓際で古い日記を見つけた。
ミナ「この日記、誰のものだろう……」
（もしかして、噂の手紙と関係があるのかな）
そのとき、金髪で制服姿のレンが勢いよく扉を開けた。
レン「ミナ！大変だ、図書室が閉まるぞ！」
ミナは小声で言った。「……静かに。先生に聞こえる」
夕日が二人を照らし、日記の最後のページがふわりとめくれた。"""
FONT = bubble_renderer.font_path(download=False) if any(__import__("os").path.isfile(p) for p in bubble_renderer.SYSTEM_FONTS) else None
needs_font = pytest.mark.skipif(FONT is None, reason="no CJK font on this machine")


def failing(*_):
    raise RuntimeError("no model")


def test_fallback_parser_extracts_story_specific_cast_and_emotions():
    script = fallback_parse(STORY, 4)
    cast = {c["name"]: c for c in script["characters"]}
    assert set(cast) == {"ミナ", "レン"}
    assert cast["ミナ"]["glasses"] is True and cast["ミナ"]["hair"] == "black hair"
    assert cast["レン"]["glasses"] is False and cast["レン"]["hair"] == "blonde hair" and cast["レン"]["clothing"] == "school uniform"
    tags = {(e["speaker"], e["emotion_tag"]) for p in script["panels"] for e in p["text_elements"]}
    assert {("ミナ", "normal"), ("ミナ", "thought"), ("レン", "shout"), ("ミナ", "whisper"), (None, "narration")} <= tags
    other = fallback_parse("ケン「行くぞ！」\nユイ「待って」", 2)
    assert [c["name"] for c in other["characters"]] == ["ケン", "ユイ"]


def test_model_output_is_normalized_and_failures_fall_back():
    payload = {"title": "t", "characters": [{"name": "A", "face": "f", "hair": "h", "clothing": "c", "glasses": True}],
               "panels": [{"visual_description": "v", "characters_present": ["A", "ghost"],
                           "text_elements": [{"speaker": "A", "text": "hi", "emotion_tag": "yell"},
                                             {"text": "n1", "emotion_tag": "narration"}, {"text": "n2", "emotion_tag": "narration"}]}]}
    out = normalize(payload, 4)
    assert out["panels"][0]["characters_present"] == ["A"]
    assert [e["emotion_tag"] for e in out["panels"][0]["text_elements"]] == ["narration", "normal"]
    assert parse_novel(STORY, 3, generator=lambda t, n: payload)["source"] == "gemini"
    fallback = parse_novel(STORY, 3, generator=failing)
    assert fallback["source"] == "fallback" and fallback["fallback_reason"] == "RuntimeError"


def test_character_sheet_is_prepended_to_every_panel_prompt():
    script = fallback_parse(STORY, 4)
    manager = CharacterManager(script["characters"])
    prompts = [manager.panel_prompt(p) for p in script["panels"]]
    assert manager.is_consistent(prompts) and all("glasses: ON" in p and "glasses: OFF" in p for p in prompts)
    assert all("Draw NO text" in p and "9:16" in p for p in prompts)
    assert not manager.is_consistent(prompts + ["SCENE: drift"])


@needs_font
def test_every_bubble_style_renders_and_avoids_the_centre():
    image = Image.new("RGB", (1080, 1920), (200, 220, 240))
    elements = [{"text": "状況説明のナレーション", "emotion_tag": "narration"},
                *({"text": f"{tag}のセリフ", "emotion_tag": tag} for tag in ("normal", "thought", "shout"))]
    out, report = bubble_renderer.render_panel(image, elements, FONT)
    assert out.size == (1080, 1920) and [r["emotion_tag"] for r in report] == ["narration", "normal", "thought", "shout"]
    for r in report[1:]:
        x0, _, x1, _ = r["box"]
        assert x1 <= 1080 * 0.52 or x0 >= 1080 * 0.48
    narration = report[0]["box"]
    assert narration[1] < 1920 * 0.15 or narration[3] > 1920 * 0.85
    with pytest.raises(ValueError):
        bubble_renderer.draw_shape(__import__("PIL.ImageDraw", fromlist=["x"]).Draw(image), "sparkle", (0, 0, 10, 10), (5, 5), 2)


@needs_font
def test_layout_moves_bubbles_away_from_detailed_areas():
    image = Image.new("RGB", (1080, 1920), (255, 255, 255))
    draw = __import__("PIL.ImageDraw", fromlist=["x"]).Draw(image)
    for x in range(0, 540, 60):
        for y in range(0, 1920, 60):
            draw.rectangle((x, y, x + 30, y + 30), fill=(0, 0, 0))
    placed = bubble_renderer.layout(image, [{"text": "x", "emotion_tag": "normal"}])
    assert placed[0][1][0] >= 540


@needs_font
def test_pipeline_outputs_numbered_vertical_panels_with_consistent_characters(tmp_path):
    prompts = []

    def art(prompt, panel, manager):
        prompts.append(prompt)
        return publisher.encode_png(Image.new("RGB", (512, 512), (90, 120, 160)))

    result = run_pipeline(STORY, tmp_path, max_panels=4, parser=failing, image_generator=art, font_file=FONT)
    manifest = result["manifest"]
    assert manifest["consistent_prompts"] and len({p.split("STYLE:")[0] for p in prompts}) == 1
    files = sorted(p.name for p in tmp_path.glob("panel_*.png"))
    assert files == [f"panel_{i:03d}.png" for i in range(1, len(manifest["panels"]) + 1)]
    for path in tmp_path.glob("panel_*.png"):
        with Image.open(path) as im:
            assert im.size == (1080, 1920)
    tags = {b["emotion_tag"] for p in manifest["panels"] for b in p["bubbles"]}
    assert {"narration", "normal", "thought", "shout", "whisper"} <= tags
    assert (tmp_path / "manifest.json").is_file()


@needs_font
def test_failed_image_generation_uses_storyboard_art_from_the_same_sheet(tmp_path):
    result = run_pipeline(STORY, None, max_panels=2, parser=failing, image_generator=failing, font_file=FONT)
    assert {p["image_engine"] for p in result["manifest"]["panels"]} == {"storyboard"}


def test_missing_font_is_a_clear_error(monkeypatch):
    from karry_agent.webtoon.pipeline import WebtoonError

    def unavailable(*_a, **_k):
        raise bubble_renderer.FontUnavailable("x")

    monkeypatch.setattr(bubble_renderer, "font_path", unavailable)
    with pytest.raises(WebtoonError) as info:
        run_pipeline(STORY)
    assert info.value.code == "font_unavailable"


@needs_font
def test_api_is_owner_only_experimental_and_serves_panels_and_zip(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    auth = {"Authorization": "Bearer synthetic-owner"}
    app = web.FastAPI()
    app.include_router(build_router(web.require_tool_auth, runner=lambda fn: fn(),
                                    pipeline=lambda text, **kw: run_pipeline(text, parser=failing, font_file=FONT, **kw)))
    client = TestClient(app)
    assert client.post("/api/webtoon/jobs", json={"novel_text": STORY}).status_code == 401
    job = client.post("/api/webtoon/jobs", json={"novel_text": STORY, "max_panels": 3}, headers=auth).json()
    done = client.get(f"/api/webtoon/jobs/{job['job_id']}", headers=auth).json()
    assert done["status"] == "succeeded" and done["consistent_prompts"] and "prompt" not in done["panels"][0]
    png = client.get(done["panels"][0]["url"], headers=auth)
    assert png.headers["content-type"] == "image/png" and Image.open(io.BytesIO(png.content)).size == (1080, 1920)
    names = zipfile.ZipFile(io.BytesIO(client.get(done["archive_url"], headers=auth).content)).namelist()
    assert names[0] == "panel_001.png" and "manifest.json" in names
    web_client = TestClient(web.app)
    monkeypatch.delenv("KARRY_EXPERIMENTAL_FEATURES")
    assert web_client.post("/api/webtoon/jobs", json={"novel_text": STORY}, headers=auth).status_code == 404
