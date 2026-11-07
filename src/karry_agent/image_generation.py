"""Gemini による画像生成ツール。

生成画像はタスクの非公開画像ストア（R2/S3 またはメモリ）へ保存し、チャットの回答に
画像 ID として添付する。存在しないファイル名・URL を返さない。
"""
from __future__ import annotations

import contextlib
import logging
import contextvars
import io
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash-image"
logger = logging.getLogger(__name__)
MAX_IMAGES_PER_RUN = 4
MAX_PROMPT_CHARS = 4000
_SAFE_STORE_ERRORS = {"ImageAttachmentError", "TaskStorageError"}

Store = Callable[[bytes, str, str], dict[str, Any]]


@dataclass
class GenerationContext:
    store: Store
    image_ids: list[str] = field(default_factory=list)
    api_calls: int = 0


_CONTEXT: contextvars.ContextVar[GenerationContext | None] = contextvars.ContextVar("karry_image_generation", default=None)


@contextlib.contextmanager
def generation_context(store: Store) -> Iterator[GenerationContext]:
    """1 回のチャット実行の間だけ、生成画像の保存先を有効にする。"""
    context = GenerationContext(store)
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def generated_ids() -> list[str]:
    context = _CONTEXT.get()
    return list(context.image_ids) if context else []


def api_calls() -> int:
    context = _CONTEXT.get()
    return context.api_calls if context else 0


def _client() -> Any:
    from .core import get_client

    return get_client()


def _inline_image(response: Any) -> tuple[bytes, str] | None:
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if isinstance(data, bytes) and data:
                return data, getattr(inline, "mime_type", None) or "image/png"
    return None


def finish_reasons(response: Any) -> list[str]:
    return [str(getattr(c, "finish_reason", "")) for c in getattr(response, "candidates", None) or []][:3]


def _to_jpeg(body: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(body)) as source:
        rgb = source.convert("RGB")
    try:
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=90)
        return out.getvalue()
    finally:
        rgb.close()


def _api_error(exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", None)
    if code == 429:
        message = "画像生成の利用上限に達しました。時間を置くか、Gemini API の利用枠・課金設定を確認してください。"
    elif code in {400, 403, 404}:
        message = "画像生成モデルを利用できません。GEMINI_IMAGE_MODEL と Gemini API キーの利用設定を確認してください。"
    else:
        message = "画像生成 API が一時的に応答しませんでした。少し待ってから再度依頼してください。"
    return {"status": "error", "error": message, "error_code": "generation_failed"}


SCENE_STYLE = ("Photorealistic, cinematic still frame, natural lighting, rich detail, shallow depth of field. "
               "No text, no letters, no captions, no watermark, no logo.")


def scene_image(prompt: str, aspect_ratio: str = "16:9") -> bytes | None:
    """One Gemini-generated JPEG for a video scene, or None (no key, API error, filtered) so callers fall back."""
    text = prompt.strip() if isinstance(prompt, str) else ""
    if not text:
        return None
    from google.genai import types

    try:
        client = _client()  # keep a reference: a collected Client closes its HTTP session mid-request
        response = client.models.generate_content(
            model=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL),
            contents=f"{text[:MAX_PROMPT_CHARS - 300]}\n\n{SCENE_STYLE}",
            config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                               image_config=types.ImageConfig(aspect_ratio=aspect_ratio)),
        )
        found = _inline_image(response)
        if not found:
            logger.warning("Gemini scene image: no image returned (finish=%s)", finish_reasons(response))
        return _to_jpeg(found[0]) if found else None
    except Exception as exc:  # noqa: BLE001 - a missing scene image must never fail the video
        logger.warning("Gemini scene image failed: %s %s", type(exc).__name__, getattr(exc, "code", ""))
        return None


def generate_image(prompt: str) -> dict[str, Any]:
    """プロンプトから画像を 1 枚生成し、タスクに保存してチャットに表示する。"""
    text = prompt.strip() if isinstance(prompt, str) else ""
    if not text:
        return {"status": "error", "error": "生成したい画像の内容を指定してください。"}
    context = _CONTEXT.get()
    if context is None:
        return {"status": "error", "error": "画像生成は Web チャットのタスク内でのみ利用できます。"}
    if len(context.image_ids) >= MAX_IMAGES_PER_RUN:
        return {"status": "error", "error": f"1 回の依頼で生成できる画像は {MAX_IMAGES_PER_RUN} 枚までです。"}

    from google.genai import errors, types

    try:
        client = _client()
        context.api_calls += 1
        response = client.models.generate_content(
            model=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_MODEL),
            contents=text[:MAX_PROMPT_CHARS],
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
    except errors.APIError as exc:
        return _api_error(exc)
    except ValueError:
        return {"status": "error", "error": "GEMINI_API_KEY が設定されていないため画像を生成できません。"}
    except Exception:
        return {"status": "error", "error": "画像生成 API に接続できませんでした。", "error_code": "generation_failed"}

    found = _inline_image(response)
    if found is None:
        return {"status": "error", "error": "画像が返されませんでした。安全フィルター等の可能性があります。内容を変えて再度依頼してください。",
                "error_code": "output_missing"}
    try:
        body = _to_jpeg(found[0])
        meta = context.store(body, "image/jpeg", f"generated-{len(context.image_ids) + 1}.jpg")
    except Exception as exc:
        message = str(exc) if type(exc).__name__ in _SAFE_STORE_ERRORS else "生成画像を保存できませんでした。"
        return {"status": "error", "error": message, "error_code": "storage_failed"}
    context.image_ids.append(meta["id"])
    return {
        "status": "success",
        "image_id": meta["id"],
        "width": meta["width"],
        "height": meta["height"],
        "message": "画像を生成して保存しました。画像はこの回答に自動で表示されます。URL・ファイルパス・Markdown の画像リンクは書かないでください。",
    }
