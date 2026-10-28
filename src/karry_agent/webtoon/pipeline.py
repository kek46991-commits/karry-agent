"""Orchestrates parse → character sheet prompts → clean art (Gemini 9:16 or local storyboard) → bubbles → panels."""
from __future__ import annotations

import hashlib
import io
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import bubble_renderer, publisher
from .character_manager import CharacterManager
from .script_parser import parse_novel

logger = logging.getLogger(__name__)
ImageGenerator = Callable[[str, dict[str, Any], CharacterManager], bytes]


class WebtoonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def gemini_image(prompt: str, panel: dict[str, Any], manager: CharacterManager) -> bytes:
    from google.genai import types

    from ..core import get_client
    from ..image_generation import DEFAULT_MODEL, _inline_image
    response = get_client().models.generate_content(
        model=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL), contents=prompt,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="9:16")))
    found = _inline_image(response)
    if found is None:
        raise WebtoonError("image_missing", "画像が返されませんでした。")
    return found[0]


def _color(seed: str, low: int = 40, high: int = 220) -> tuple[int, int, int]:
    d = hashlib.sha256(seed.encode()).digest()
    return tuple(low + d[i] % (high - low) for i in range(3))


HAIR_COLORS = {"black": (25, 25, 30), "blonde": (235, 200, 90), "silver": (200, 205, 215), "white": (240, 240, 240),
               "brown": (110, 70, 40), "red": (190, 50, 40), "blue": (40, 70, 160), "auburn": (150, 60, 30)}


def storyboard_image(prompt: str, panel: dict[str, Any], manager: CharacterManager) -> bytes:
    """Key-free stand-in art: every character is drawn from the same sheet traits in every panel."""
    w, h = publisher.WIDTH, publisher.HEIGHT
    top, bottom = _color(panel.get("visual_description", "") + "sky", 120, 230), _color(panel.get("visual_description", "") + "ground", 60, 160)
    image = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(image)
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)))
    present = [manager.characters[n] for n in panel.get("characters_present") or [] if n in manager.characters]
    for i, c in enumerate(present[:3]):
        cx = int(w * (i + 1) / (len(present[:3]) + 1))
        head_r, cy = int(w * 0.09), int(h * 0.48)
        hair = next((rgb for key, rgb in HAIR_COLORS.items() if key in c.get("hair", "").lower()), _color(c["name"] + "hair"))
        draw.rounded_rectangle((cx - int(w * 0.13), cy + head_r, cx + int(w * 0.13), int(h * 0.83)), radius=40,
                               fill=_color(c["name"] + c.get("clothing", "")))
        draw.ellipse((cx - head_r, cy - head_r, cx + head_r, cy + head_r), fill=(250, 225, 205), outline=(60, 40, 30), width=4)
        draw.pieslice((cx - head_r - 6, cy - head_r - 10, cx + head_r + 6, cy + head_r // 2), 180, 360, fill=hair)
        eye_y = cy + head_r // 6
        for dx in (-head_r // 2.6, head_r // 2.6):
            draw.ellipse((cx + dx - 8, eye_y - 8, cx + dx + 8, eye_y + 8), fill=(30, 30, 40))
            if c.get("glasses"):
                draw.ellipse((cx + dx - 26, eye_y - 22, cx + dx + 26, eye_y + 22), outline=(20, 20, 20), width=6)
        if c.get("glasses"):
            draw.line((cx - head_r // 2.6 + 26, eye_y, cx + head_r // 2.6 - 26, eye_y), fill=(20, 20, 20), width=6)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def default_image_generator() -> tuple[ImageGenerator, str]:
    if os.getenv("GEMINI_API_KEY") and os.getenv("KARRY_WEBTOON_IMAGES", "gemini") == "gemini":
        return gemini_image, "gemini"
    return storyboard_image, "storyboard"


def run_pipeline(text: str, out_dir: Path | None = None, *, max_panels: int = 6, parser: Callable | None = None,
                 image_generator: ImageGenerator | None = None, font_file: str | None = None,
                 progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    try:
        font = font_file or bubble_renderer.font_path()
    except bubble_renderer.FontUnavailable as exc:
        raise WebtoonError("font_unavailable", "日本語フォントを用意できませんでした。") from exc
    try:
        script = parse_novel(text, max_panels, generator=parser)
    except ValueError as exc:
        raise WebtoonError("novel_required", "小説の本文を入力してください。") from exc
    manager = CharacterManager(script["characters"])
    generator, engine = (image_generator, "custom") if image_generator else default_image_generator()
    panels, records = [], []
    for index, panel in enumerate(script["panels"], start=1):
        prompt = manager.panel_prompt(panel)
        try:
            art, art_engine = generator(prompt, panel, manager), engine
        except Exception as exc:  # noqa: BLE001 - one failed image falls back to the storyboard art
            logger.warning("Webtoon panel %d image fallback: %s", index, type(exc).__name__)
            art, art_engine = storyboard_image(prompt, panel, manager), "storyboard"
        with Image.open(io.BytesIO(art)) as raw:
            vertical = publisher.to_vertical(raw)
        finished, bubbles = bubble_renderer.render_panel(vertical, panel["text_elements"], font)
        panels.append(publisher.encode_png(finished))
        records.append({"index": index, "file": publisher.panel_name(index), "size": list(finished.size), "image_engine": art_engine,
                        "characters_present": panel["characters_present"], "text_elements": panel["text_elements"],
                        "bubbles": bubbles, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "prompt": prompt})
        if progress:
            progress(index, len(script["panels"]))
    manifest = {"title": script["title"], "parser": script["source"], "fallback_reason": script.get("fallback_reason"),
                "characters": script["characters"], "character_sheet": manager.sheet,
                "character_sheet_sha256": manager.fingerprint,
                "consistent_prompts": manager.is_consistent([r["prompt"] for r in records]),
                "aspect_ratio": "9:16", "panels": records}
    if out_dir is not None:
        publisher.publish(panels, manifest, out_dir)
    return {"manifest": manifest, "panels": panels}
