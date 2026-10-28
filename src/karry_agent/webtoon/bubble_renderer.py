"""Programmatic speech bubbles and narration boxes drawn with Pillow onto clean panel art."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

FONT_URL = "https://github.com/notofonts/noto-cjk/raw/Sans2.004/Sans/SubsetOTF/JP/NotoSansJP-Bold.otf"
FONT_SHA256 = "1b0edfb500b73a4fa8a4fcaae1bbbd403994e08e73e3e0da37e70d3853f42c5f"
SYSTEM_FONTS = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf")
INK, PAPER = (20, 20, 20), (255, 255, 255)


class FontUnavailable(RuntimeError):
    pass


def font_path(download: bool = True) -> str:
    for path in (os.getenv("KARRY_WEBTOON_FONT", ""), *SYSTEM_FONTS):
        if path and os.path.isfile(path):
            return path
    cache = Path(os.getenv("KARRY_WEBTOON_FONT_CACHE", Path.home() / ".cache" / "karry-agent" / "fonts")) / "NotoSansJP-Bold.otf"
    if cache.is_file() and hashlib.sha256(cache.read_bytes()).hexdigest() == FONT_SHA256:
        return str(cache)
    if not download:
        raise FontUnavailable("Japanese font not found")
    import requests
    try:
        response = requests.get(FONT_URL, timeout=(5, 60))
        response.raise_for_status()
    except Exception as exc:
        raise FontUnavailable("Japanese font download failed") from exc
    if hashlib.sha256(response.content).hexdigest() != FONT_SHA256:
        raise FontUnavailable("Japanese font checksum mismatch")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(response.content)
    return str(cache)


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int = 6) -> list[str]:
    lines, line = [], ""
    for char in text.replace("\n", ""):
        if font.getlength(line + char) > width and line:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def busy_map(image: Image.Image, cols: int = 3, rows: int = 6) -> list[list[float]]:
    """Edge density per grid cell: high values mean detailed art (faces, glasses) to avoid."""
    small = image.convert("L").resize((cols * 40, rows * 40)).filter(ImageFilter.FIND_EDGES)
    return [[ImageStat.Stat(small.crop((c * 40, r * 40, c * 40 + 40, r * 40 + 40))).mean[0] for c in range(cols)]
            for r in range(rows)]


def layout(image: Image.Image, elements: list[dict[str, Any]]) -> list[tuple[dict[str, Any], tuple[int, int, int, int]]]:
    """Narration → quieter of the top/bottom bands; bubbles → quietest side slots, never the centre column."""
    w, h = image.size
    grid = busy_map(image)
    placed = []
    narration = [e for e in elements if e["emotion_tag"] == "narration"]
    bubbles = [e for e in elements if e["emotion_tag"] != "narration"]
    used_band = None
    if narration:
        top, bottom = sum(grid[0]), sum(grid[-1])
        used_band = "top" if top <= bottom else "bottom"
        box_h = int(h * 0.12)
        y = int(h * 0.02) if used_band == "top" else h - box_h - int(h * 0.02)
        placed.append((narration[0], (int(w * 0.04), y, int(w * 0.96), y + box_h)))
    slots = []
    for row in range(6):
        band = "top" if row == 0 else "bottom" if row == 5 else None
        if band and band == used_band:
            continue
        for col in (0, 2):
            score = grid[row][col] + 0.5 * grid[row][1]
            slots.append((score, row, col))
    slots.sort(key=lambda s: (round(s[0] / 8), s[1]))
    chosen = sorted(slots[:len(bubbles)], key=lambda s: (s[1], s[2]))
    for element, (_, row, col) in zip(bubbles, chosen):
        bw, bh = int(w * 0.46), int(h * 0.15)
        cx = int(w * (0.27 if col == 0 else 0.73))
        cy = int(h * (row + 0.5) / 6)
        x0, y0 = max(int(w * 0.02), cx - bw // 2), max(int(h * 0.01), cy - bh // 2)
        placed.append((element, (x0, y0, min(w - int(w * 0.02), x0 + bw), min(h - int(h * 0.01), y0 + bh))))
    return placed


def _ellipse_points(box, n=72, jitter=None):
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    return [(cx + rx * (jitter(i) if jitter else 1) * math.cos(2 * math.pi * i / n),
             cy + ry * (jitter(i) if jitter else 1) * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _tail(draw, box, toward, width):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    tx, ty = toward
    base = [(cx - (x1 - x0) * 0.08, cy), (cx + (x1 - x0) * 0.08, cy)]
    tip = (cx + (tx - cx) * 0.55, cy + (ty - cy) * 0.55 if abs(ty - cy) > 1 else y1 + (y1 - y0) * 0.35)
    draw.polygon([*base, tip], fill=PAPER, outline=INK, width=width)


def draw_shape(draw: ImageDraw.ImageDraw, tag: str, box, toward, width: int) -> None:
    x0, y0, x1, y1 = box
    if tag == "normal":
        _tail(draw, box, toward, width)
        draw.ellipse(box, fill=PAPER, outline=INK, width=width)
    elif tag == "thought":
        rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
        for px, py in _ellipse_points((x0 + rx * 0.12, y0 + ry * 0.15, x1 - rx * 0.12, y1 - ry * 0.15), n=14):
            r = min(rx, ry) * 0.32
            draw.ellipse((px - r, py - r, px + r, py + r), fill=PAPER, outline=INK, width=width)
        draw.ellipse((x0 + rx * 0.15, y0 + ry * 0.2, x1 - rx * 0.15, y1 - ry * 0.2), fill=PAPER)
        cx, cy = (x0 + x1) / 2, y1
        for step, r in ((0.12, ry * 0.16), (0.3, ry * 0.1)):
            px, py = cx + (toward[0] - cx) * step, cy + ry * (0.25 + step)
            draw.ellipse((px - r, py - r, px + r, py + r), fill=PAPER, outline=INK, width=max(2, width - 1))
    elif tag == "shout":
        points = _ellipse_points((x0 - (x1 - x0) * 0.05, y0 - (y1 - y0) * 0.1, x1 + (x1 - x0) * 0.05, y1 + (y1 - y0) * 0.1),
                                 n=36, jitter=lambda i: 1.0 if i % 2 == 0 else 0.78)
        draw.polygon(points, fill=PAPER, outline=INK, width=width + 1)
    elif tag == "whisper":
        draw.ellipse(box, fill=PAPER)
        points = _ellipse_points(box, n=60)
        for i in range(0, len(points), 2):
            draw.line([points[i], points[(i + 1) % len(points)]], fill=INK, width=max(2, width - 1))
    else:
        raise ValueError(f"unknown bubble style {tag}")


def render_panel(image: Image.Image, elements: list[dict[str, Any]], font_file: str | None = None) -> tuple[Image.Image, list[dict[str, Any]]]:
    base = image.convert("RGBA")
    w, h = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    size = max(18, w // 26)
    font = ImageFont.truetype(font_file or font_path(), size)
    stroke = max(3, w // 300)
    report = []
    for element, box in layout(base, elements):
        tag = element["emotion_tag"]
        x0, y0, x1, y1 = box
        if tag == "narration":
            draw.rectangle(box, fill=(10, 10, 20, 170))
            lines = wrap(element["text"], font, int((x1 - x0) * 0.9), 3)
            color = (255, 255, 255, 255)
        else:
            draw_shape(draw, tag, box, (w / 2, h / 2), stroke)
            lines = wrap(element["text"], font, int((x1 - x0) * (0.62 if tag == "shout" else 0.7)), 4)
            color = (*INK, 255)
        line_h = int(size * 1.25)
        ty = (y0 + y1) / 2 - line_h * len(lines) / 2
        for line in lines:
            tw = font.getlength(line)
            draw.text(((x0 + x1) / 2 - tw / 2, ty), line, font=font, fill=color)
            ty += line_h
        report.append({"emotion_tag": tag, "box": list(box), "lines": lines})
    return Image.alpha_composite(base, overlay).convert("RGB"), report
