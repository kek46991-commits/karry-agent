"""Chat photo intake: product photos (+ Rakumart / 1688 URL) → Gemini name + polish → legal review → KERRY shop.

Triggered by the chat's 「商品写真として登録」 switch (``product_draft``) or, with photos attached, by wording such as
「出品」「下書き」「商品写真」. A Rakumart / 1688 URL, 原価（元） and 商品名 in the message are used when present; a missing
title and category come from Gemini Vision. When the legal review is SAFE and a price could be set, the draft is
published right away (``KARRY_CHAT_AUTO_PUBLISH=0`` keeps it as a private /admin draft). ``KARRY_DEFAULT_COST_CNY``
is the cost used when neither the message nor the 1688 page gives one.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from typing import Any

from . import alibaba1688 as ali
from .core import AgentMetrics, AgentResult, ExecutionEvent

ACTION = "create_product_draft"
_INTENT = re.compile(r"出品|下書き|商品写真|商品画像|ショップに|仕入れ|1688|ラクマート|rakumart", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']*(?:1688\.com|rakumart(?:-[a-z0-9]+)?\.(?:com|cn))/[^\s<>\"']+", re.IGNORECASE)
_COST = re.compile(r"(?:原価|仕入れ値|仕入値|価格)\s*[:：は]?\s*(\d+(?:\.\d+)?)\s*(?:元|RMB|CNY|人民元)?|"
                   r"(\d+(?:\.\d+)?)\s*(?:元|RMB|CNY|人民元)", re.IGNORECASE)
_TITLE = re.compile(r"(?:商品名|タイトル)\s*[:：は]?\s*([^\n]+)")
_CATEGORY = re.compile(r"カテゴリー?\s*[:：は]?\s*([^\n\s、,]+)")

CreateDraft = Callable[..., dict[str, Any]]  # (EvaluateRequest, photo_bytes, default_cost=) -> evaluate result
Publish = Callable[..., dict[str, Any]]  # (draft_id, via=) -> publish result


def auto_publish_enabled() -> bool:
    return os.getenv("KARRY_CHAT_AUTO_PUBLISH", "1").strip().lower() not in ("0", "false", "no", "off")


def default_cost() -> float | None:
    try:
        value = float(os.getenv("KARRY_DEFAULT_COST_CNY", "") or "nan")
    except ValueError:
        return None
    return value if 0 < value <= 100_000 else None


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


def _summary(result: dict[str, Any], published: dict[str, Any] | None = None) -> str:
    product, review, pricing = result["product"], result["review"], result["pricing"]
    info = product.get("image_processing") or {}
    sources, enhanced = int(info.get("sources") or 0), int(info.get("enhanced") or 0)
    listing = (published or {}).get("listing")
    if listing:
        lines = [f"✅ ショップに掲載しました（掲載中の商品・ショップ ID {listing['id']}）。"]
    else:
        lines = ["🛍 商品写真を下書きに保存しました（管理画面 /admin の「下書き」に出ます。まだショップには載せていません）。"]
        if published and published.get("reason"):
            lines.append(f"⏸ 自動掲載しなかった理由: {published['reason']}")
    lines.append(f"🏷 商品名: {product.get('title') or ''}" + (f"｜カテゴリー: {product['category']}" if product.get("category") else ""))
    if enhanced:
        lines.append(f"🎨 Gemini で写真 {enhanced}/{sources} 枚の光と背景を整えました。")
    elif sources:
        lines.append("⚠ Gemini で写真を加工できなかったため、元の写真で保存しました。")
    if sources:
        lines.append(f"🖼 写真 {sources} 枚を保存しました（1 枚目がメイン画像）。")
    lines.append(f"⚖ 法的審査: {review['verdict']}" + ("（自動掲載完了）" if listing else ""))
    if pricing:
        lines.append(f"💴 販売価格: {int(pricing['selling_price_jpy']):,} 円" + ("（デフォルト原価で計算）" if product.get("default_cost") else ""))
    else:
        lines.append("💴 販売価格: 未計算（「原価 25元」のように原価を書いて送ると計算します）")
    if product.get("title_missing"):
        lines.append("📝 商品名が未入力です（「商品名: 木製ブックスタンド」のように書いて送ってください）。")
    if listing:
        lines.append(f"{LISTING_MARKER}{listing['id']}")
    else:
        lines.append(draft_marker(result["draft"]["id"]))
    return "\n".join(lines)


LISTING_MARKER = "掲載商品ID: "


def run(create: CreateDraft, prompt: str, images: list[dict[str, Any]] | None, publish: Publish | None = None) -> AgentResult:
    from .alibaba_api import EvaluateRequest

    started = time.perf_counter()
    photos = [item["data"] for item in images or [] if isinstance(item.get("data"), bytes)]
    fields = parse_fields(prompt)
    arguments = {"photos": len(photos), "with_url": bool(fields["product"]), "with_cost": fields["cost_cny"] is not None,
                 "auto_publish": publish is not None}
    if not photos:
        observation = {"status": "error", "code": "photo_required"}
        text, status, errors = "商品写真を添付して送ってください。写真を Gemini で整えてショップに登録します。", "failed", 1
    else:
        try:
            result = create(EvaluateRequest(**fields), photos, default_cost=default_cost())
            published = publish(result["draft"]["id"], via="chat") if publish else None
            listing = (published or {}).get("listing")
            observation = {"status": "completed", "draft_id": None if listing else result["draft"]["id"],
                           "listing_id": (listing or {}).get("id"), "published": bool(listing),
                           "verdict": result["review"]["verdict"], "image_processing": result["product"].get("image_processing")}
            text, status, errors = _summary(result, published), "completed", 0
        except ali.AlibabaError as exc:
            observation = {"status": "error", "code": exc.code}
            text, status, errors = f"登録できませんでした: {exc.message}", "failed", 1
        except Exception:  # noqa: BLE001 - a shop outage must not fail the chat turn
            observation = {"status": "error", "code": "draft_unavailable"}
            text, status, errors = "登録できませんでした。時間をおいてもう一度お試しください。", "failed", 1
    elapsed = (time.perf_counter() - started) * 1000
    event = ExecutionEvent(1, ACTION, arguments, observation, elapsed)
    return AgentResult(text, 1, 1, [event], AgentMetrics(tool_calls=1, errors=errors, elapsed_ms=elapsed), status=status)
