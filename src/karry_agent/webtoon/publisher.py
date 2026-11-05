"""9:16 webtoon formatter: cover-crop to vertical, number panels, write a manifest and a ZIP."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

WIDTH, HEIGHT = 1080, 1920


def to_vertical(image: Image.Image, width: int = WIDTH) -> Image.Image:
    return ImageOps.fit(image.convert("RGB"), (width, width * 16 // 9), method=Image.Resampling.LANCZOS)


def panel_name(index: int) -> str:
    return f"panel_{index:03d}.png"


def encode_png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def publish(panels: list[bytes], manifest: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, body in enumerate(panels, start=1):
        path = out_dir / panel_name(index)
        path.write_bytes(body)
        paths.append(path)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


def archive(panels: list[bytes], manifest: dict[str, Any]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, body in enumerate(panels, start=1):
            zf.writestr(panel_name(index), body)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return out.getvalue()
