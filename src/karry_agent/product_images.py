"""Shop product photos: owner uploads and 1688 (alicdn) page photos, polished by Gemini (light / background only).

The product itself must stay the same (shape, colour, parts); only lighting and background change. Every Gemini
failure falls back to the original photo so a listing is never left without pictures.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
import re
from collections.abc import Callable
from typing import Any

from .alibaba1688 import AlibabaError

logger = logging.getLogger(__name__)
MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_ENHANCE = 3
MAX_SHOP_IMAGE_BYTES = 900_000
ALICDN = re.compile(r"^https://(?:cbu01|img)\.alicdn\.com/")
ENHANCE_PROMPT = (
    "Edit this product photo for an online shop. Keep the product itself exactly the same: same shape, colours, "
    "materials, size, parts, printed patterns and quantity. Do not add, remove or redesign anything on the product. "
    "Only improve lighting, white balance and sharpness, and replace the background with a clean, softly lit studio "
    "or tasteful lifestyle backdrop that suits this product: {title}. Remove watermarks, price tags and text overlays "
    "that are not printed on the product itself. Square composition, product centred, photorealistic."
)

Editor = Callable[[bytes, str], "bytes | None"]


def _jpeg(raw: bytes, side: int = 1600) -> bytes:
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 40_000_000:
                raise AlibabaError("写真の解像度が大きすぎます。", 413, "invalid_photo")
            rgb = ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise AlibabaError("写真の形式を読めませんでした（JPG / PNG / WEBP）。", 422, "invalid_photo") from None
    rgb.thumbnail((side, side))
    out = io.BytesIO()
    rgb.save(out, "JPEG", quality=88)
    return out.getvalue()


def decode_photo(value: str) -> bytes:
    """Owner-uploaded data URL / base64 → validated JPEG (max 1600 px)."""
    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise AlibabaError("写真のデータを読めませんでした。", 422, "invalid_photo") from None
    if not 0 < len(raw) <= MAX_PHOTO_BYTES:
        raise AlibabaError("写真は 1 枚 8MB までです。", 413, "invalid_photo")
    return _jpeg(raw)


def download(url: str, session: Any = None) -> bytes | None:
    """One 1688 (alicdn) product photo as JPEG, or None. Only alicdn over https, no redirects, 8 MB cap."""
    if not isinstance(url, str) or not ALICDN.match(url):
        return None
    import requests
    http = session or requests
    try:
        response = http.get(url, timeout=(5, 15), stream=True, allow_redirects=False,
                            headers={"User-Agent": "Mozilla/5.0 (compatible; KERRY-shop/1.0)"})
        try:
            if response.status_code != 200:
                return None
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_PHOTO_BYTES:
                    return None
        finally:
            response.close()
        return _jpeg(bytes(body))
    except Exception:  # noqa: BLE001 - an unreachable photo is skipped, never an error
        return None


def gemini_enhance(photo: bytes, title: str) -> bytes | None:
    """Gemini image edit (GEMINI_IMAGE_MODEL): better light and background, same product. None on any failure."""
    from google.genai import types

    from . import image_generation as ig
    try:
        client = ig._client()  # keep a reference: a collected Client closes its HTTP session mid-request
        response = client.models.generate_content(
            model=os.getenv("GEMINI_IMAGE_MODEL", ig.DEFAULT_MODEL),
            contents=[types.Part.from_bytes(data=photo, mime_type="image/jpeg"),
                      ENHANCE_PROMPT.format(title=(title or "general goods")[:200])],
            config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                               image_config=types.ImageConfig(aspect_ratio="1:1")),
        )
        found = ig._inline_image(response)
        if not found:
            logger.warning("Gemini photo edit: no image returned (finish=%s)", ig.finish_reasons(response))
        return ig._to_jpeg(found[0]) if found else None
    except Exception as exc:  # noqa: BLE001 - keep the original photo instead
        logger.warning("Gemini photo edit failed: %s %s", type(exc).__name__, getattr(exc, "code", ""))
        return None


def shop_jpeg(body: bytes, limit: int = MAX_SHOP_IMAGE_BYTES) -> bytes:
    """Shrink a JPEG until it fits the storage document limit."""
    from PIL import Image
    with Image.open(io.BytesIO(body)) as source:
        image = source.convert("RGB")
    side = 1200
    while True:
        copy = image.copy()
        copy.thumbnail((side, side))
        out = io.BytesIO()
        copy.save(out, "JPEG", quality=82)
        if out.tell() <= limit or side <= 300:
            return out.getvalue()
        side = int(side * 0.75)


def prepare(product: dict[str, Any], photos: list[str], enhance: bool, store: Any,
            editor: Editor | None = None, session: Any = None) -> dict[str, Any]:
    """Store polished shop images for a draft and put them first in ``product["images"]``."""
    edit = editor or gemini_enhance
    page = [u for u in product.get("images") or [] if isinstance(u, str) and ALICDN.match(u)]
    sources = [(decode_photo(p), None) for p in photos[:MAX_PHOTOS]]
    used: set[str] = set()
    if enhance:
        for url in page:
            if len(sources) >= MAX_ENHANCE:
                break
            body = download(url, session)
            if body:
                sources.append((body, url))
                used.add(url)
    stored, enhanced = [], 0
    for position, (body, _) in enumerate(sources):
        result = edit(body, product.get("title") or "") if enhance and position < MAX_ENHANCE else None
        enhanced += bool(result)
        stored.append(store.save_image(shop_jpeg(result or body)))
    product["images"] = (stored + [u for u in page if u not in used])[:12]
    info = {"engine": "gemini" if enhance else None, "sources": len(sources), "enhanced": enhanced}
    product["image_processing"] = info
    return info
