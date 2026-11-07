"""Chat photo intake: product photos sent in chat → Gemini light/background polish → private /admin draft.

Triggered by the chat's 「商品写真として下書き保存」 switch (``product_draft``) or, with photos attached, by wording
such as 「出品」「下書き」「商品写真」. A 1688 URL, 原価（元） and 商品名 in the message are used when present.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from . import alibaba1688 as ali
from .core import AgentMetrics, AgentResult, ExecutionEvent

ACTION = "create_product_draft"
_INTENT = re.compile(r"出品|下書き|商品写真|商品画像|ショップに|仕入れ|1688|ラクマート|rakumart", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']*(?:1688\.com|rakumart\.(?:com|cn))/[^\s<>\"']+")
_COST = re.compile(r"(?:原価|仕入れ値|仕入値|価格)\s*[:：は]?\s*(\d+(?:\.\d+)?)\s*(?:元|RMB|CNY|人民元)?|"
                   r"(\d+(?:\.\d+)?)\s*(?:元|RMB|CNY|人民元)", re.IGNORECASE)
_TITLE = re.compile(r"(?:商品名|タイトル)\s*[:：は]?\s*([^\n]+)")
_CATEGORY = re.compile(r"カテゴリー?\s*[:：は]?\s*([^\n\s、,]+)")

CreateDraft = Callable[..., dict[str, Any]]  # (EvaluateRequest, photo_bytes) -> evaluate result


def is_product_request(text: str) -> bool:
    return bool(_INTENT.search(text or ""))


def parse_fields(text: str) -> dict[str, Any]:
    text = text or ""
    url = _URL.search(text)
    fields: dict[str, Any] = {"product": url.group(0) if url else "", "cost_cny": None, "title": "", "category": ""}
    cost = _COST.search(_URL.sub(" ", text))
    if cost:
        value = float(cost.group(1) or cost.group(2))
        fields["cost_cny"] = value if 0 < value <= 100_000 else None
    title = _TITLE.search(text)
    if title:
        fields["title"] = re.split(r"\s+(?:原価|仕入れ値|仕入値|価格|カテゴリー?)\s*[:：は]?", title.group(1))[0].strip(" 、。,")[:200]
    category = _CATEGORY.search(text)
    if category:
        fields["category"] = category.group(1)[:80]
    return fields


def draft_marker(draft_id: str) -> str:
    return f"商品下書きID: {draft_id}"


def _summary(result: dict[str, Any]) -> str:
    product, review, pricing = result["product"], result["review"], result["pricing"]
    info = product.get("image_processing") or {}
    sources, enhanced = int(info.get("sources") or 0), int(info.get("enhanced") or 0)
    lines = ["🛍 商品写真を下書きに保存しました（管理画面 /admin の「下書き」に出ます。まだショップには載せていません）。"]
    if enhanced:
        lines.append(f"🎨 Gemini で写真 {enhanced}/{sources} 枚の光と背景を整えました。")
    elif sources:
        lines.append("⚠ Gemini で写真を加工できなかったため、元の写真で保存しました。")
    lines.append(f"⚖ 法的審査: {review['verdict']}")
    lines.append(f"💴 販売価格: {int(pricing['selling_price_jpy']):,} 円" if pricing else
                 "💴 販売価格: 未計算（「原価 25元」のように原価を書いて送ると計算します）")
    if product.get("title_missing"):
        lines.append("📝 商品名が未入力です（「商品名: 木製ブックスタンド」のように書いて送ってください）。")
    lines.append(draft_marker(result["draft"]["id"]))
    return "\n".join(lines)


def run(create: CreateDraft, prompt: str, images: list[dict[str, Any]] | None) -> AgentResult:
    from .alibaba_api import EvaluateRequest

    started = time.perf_counter()
    photos = [item["data"] for item in images or [] if isinstance(item.get("data"), bytes)]
    fields = parse_fields(prompt)
    arguments = {"photos": len(photos), "with_url": bool(fields["product"]), "with_cost": fields["cost_cny"] is not None}
    if not photos:
        observation = {"status": "error", "code": "photo_required"}
        text, status, errors = "商品写真を添付して送ってください。写真を Gemini で整えて下書きに保存します。", "failed", 1
    else:
        try:
            result = create(EvaluateRequest(**fields), photos)
            observation = {"status": "completed", "draft_id": result["draft"]["id"], "verdict": result["review"]["verdict"],
                           "image_processing": result["product"].get("image_processing")}
            text, status, errors = _summary(result), "completed", 0
        except ali.AlibabaError as exc:
            observation = {"status": "error", "code": exc.code}
            text, status, errors = f"下書きに保存できませんでした: {exc.message}", "failed", 1
        except Exception:  # noqa: BLE001 - a shop outage must not fail the chat turn
            observation = {"status": "error", "code": "draft_unavailable"}
            text, status, errors = "下書きに保存できませんでした。時間をおいてもう一度お試しください。", "failed", 1
    elapsed = (time.perf_counter() - started) * 1000
    event = ExecutionEvent(1, ACTION, arguments, observation, elapsed)
    return AgentResult(text, 1, 1, [event], AgentMetrics(tool_calls=1, errors=errors, elapsed_ms=elapsed), status=status)
