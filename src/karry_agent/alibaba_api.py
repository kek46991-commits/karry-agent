"""Owner-authenticated JSON API: 1688 URL / screenshot intake → legal screening → JPY pricing → KERRY shop."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import alibaba1688 as ali
from . import alibaba_scrape, product_images, shop_store


class PricingOverrides(BaseModel):
    cny_jpy_rate: float | None = None
    agent_fee_rate: float | None = None
    shipping_jpy: float | None = None
    markup: float | None = None


class FetchRequest(BaseModel):
    product: str = Field(default="", max_length=500, description="1688 商品 URL または商品 ID（チャットの写真だけなら空）")
    cost_cny: float | None = Field(default=None, gt=0, le=100000, description="原価（人民元）の手入力")
    title: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=80)


class EvaluateRequest(FetchRequest):
    pricing: PricingOverrides = Field(default_factory=PricingOverrides)
    photos: list[Annotated[str, Field(max_length=12_000_000)]] = Field(
        default_factory=list, max_length=product_images.MAX_PHOTOS, description="商品写真（data URL）。OCR はしません")
    enhance_images: bool = Field(default=True, description="Gemini で光と背景を整える")
    image_urls: list[Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=list, max_length=product_images.MAX_PHOTOS,
        description="ブラウザ（ラクマートのブックマーク）で読んだ商品写真の URL。alicdn のみ")


class PublishRequest(BaseModel):
    draft_id: str = Field(pattern=r"^[a-f0-9]{32}$")


logger = logging.getLogger(__name__)
MAX_LOG_ENTRIES = 200


class AuditLog:
    """In-process ring buffer for the legal dashboard (owner-only; cleared on restart). Holds no credentials."""

    def __init__(self, size: int = MAX_LOG_ENTRIES):
        self._entries: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, **entry: Any) -> dict[str, Any]:
        record = {"id": uuid.uuid4().hex, "at": time.time(), **entry}
        with self._lock:
            self._entries.appendleft(record)
        logger.info("1688 %s product=%s verdict=%s code=%s", record.get("action"), record.get("product_id"),
                    record.get("verdict"), record.get("code"))
        return record

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)[:limit]


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(exc: ali.AlibabaError) -> JSONResponse:
    return _json({"detail": exc.message, "code": exc.code}, exc.status_code)


def build_router(auth: Callable[..., Any], intake: Callable[..., dict[str, Any]] | None = None,
                 reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 audit: AuditLog | None = None, store_factory: Callable[[], Any] | None = None,
                 image_pipeline: Callable[..., dict[str, Any]] | None = None,
                 namer: Callable[[list[bytes]], dict[str, str] | None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/alibaba", tags=["alibaba"], dependencies=[Depends(auth)])
    read = intake or alibaba_scrape.intake
    review = reviewer or ali.review_product
    log = audit or AuditLog()
    stores = store_factory or shop_store.ShopStore
    photos = image_pipeline or product_images.prepare
    describe = namer or product_images.gemini_describe
    router.audit = log

    def safe_id(product: str) -> str | None:
        try:
            return ali.parse_product_id(product)
        except ali.AlibabaError:
            return None

    def fetch(request: FetchRequest) -> dict[str, Any]:
        return read(request.product, request.cost_cny, request.title, request.category)

    def summary(product: dict[str, Any]) -> dict[str, Any]:
        return {"product_id": product.get("product_id"), "title": product.get("title"), "images": len(product.get("images") or [])}

    def checks(review_result: dict[str, Any]) -> dict[str, Any]:
        return {k: {"status": v["status"], "reason": v["reason"]} for k, v in review_result["checks"].items()}

    @router.get("/config")
    def config():
        return _json({"pricing": ali.pricing_defaults(), "checks": ali.CHECK_LABELS})

    @router.get("/logs")
    def logs(limit: int = 50):
        return _json({"entries": log.list(max(1, min(limit, MAX_LOG_ENTRIES)))})

    @router.post("/fetch")
    async def fetch_product(request: FetchRequest):
        try:
            product = await run_in_threadpool(fetch, request)
            log.add(action="fetch", status="ok", **summary(product))
            return _json({"product": product})
        except ali.AlibabaError as exc:
            log.add(action="fetch", status="error", product_id=safe_id(request.product), code=exc.code, message=exc.message)
            return _error(exc)

    def name_from_photos(product: dict[str, Any], sources: list[str | bytes]) -> None:
        """Gemini Vision fills a missing title (and category) from the photos."""
        decoded = []
        for item in sources[:3]:
            try:
                decoded.append(product_images.decode_photo(item) if isinstance(item, str) else product_images.decode_bytes(item))
            except ali.AlibabaError:
                continue
        named = describe(decoded) if decoded else None
        if not named:
            return
        product["title"], product["title_missing"] = named["title"], False
        if named.get("category") and not product.get("category"):
            product["category"] = named["category"]
        product["notes"] = [n for n in product.get("notes") or [] if "仮の名前" not in n] + ["商品名とカテゴリーは Gemini が写真から付けました。"]

    def intake_product(request: EvaluateRequest, default_cost: float | None) -> dict[str, Any]:
        if not request.product.strip():
            return alibaba_scrape.photo_product(request.cost_cny, request.title, request.category)
        try:
            return fetch(request)
        except ali.AlibabaError as exc:
            if default_cost is None or request.cost_cny is not None or exc.code not in ("page_blocked", "page_unavailable",
                                                                                         "product_not_found"):
                raise
            product = fetch(request.model_copy(update={"cost_cny": default_cost}))
            product["default_cost"] = True
            product["notes"] = [*product.get("notes", []), "原価が分からないため、設定のデフォルト原価を使いました。"]
            return product

    def draft_work(request: EvaluateRequest, photo_bytes: list[bytes] | None = None,
                   default_cost: float | None = None) -> dict[str, Any]:
        product = intake_product(request, default_cost)
        if product.get("min_price_cny") is None and default_cost is not None:
            product["min_price_cny"] = product["max_price_cny"] = round(float(default_cost), 2)
            product["default_cost"] = True
            product["notes"] = [*product.get("notes", []), "原価が分からないため、設定のデフォルト原価を使いました。"]
        if request.image_urls and not product.get("images"):
            product["images"] = [u for u in map(ali.image_url, request.image_urls) if u][:product_images.MAX_PHOTOS]
        sources = [*(photo_bytes or []), *request.photos][:product_images.MAX_PHOTOS]
        if product.get("title_missing") and sources:
            name_from_photos(product, sources)
        photos(product, sources, request.enhance_images, stores())
        review_result = review(product)
        pricing, pricing_error = None, None
        try:
            pricing = ali.price_product(product.get("min_price_cny"), request.pricing.model_dump(exclude_none=True))
        except ali.AlibabaError as exc:
            if exc.code == "invalid_pricing":
                raise
            pricing_error = "原価（元）を入力すると販売価格を計算できます。"
        draft = stores().save_draft({"product": product, "review": review_result, "pricing": pricing})
        return {"draft": draft, "product": product, "review": review_result, "pricing": pricing, "pricing_error": pricing_error}

    def create_draft(request: EvaluateRequest, photo_bytes: list[bytes] | None = None,
                     default_cost: float | None = None) -> dict[str, Any]:
        """Shared by /evaluate and chat photo intake. Raises AlibabaError (logged) on bad input."""
        try:
            result = draft_work(request, photo_bytes, default_cost)
        except ali.AlibabaError as exc:
            log.add(action="evaluate", status="error", product_id=safe_id(request.product), code=exc.code, message=exc.message)
            raise
        review_result = result["review"]
        result["log"] = log.add(action="evaluate", status="ok", **summary(result["product"]), verdict=review_result["verdict"],
                                engine=review_result["engine"], checks=checks(review_result),
                                selling_price_jpy=(result["pricing"] or {}).get("selling_price_jpy"))
        return result

    router.create_draft = create_draft

    @router.post("/evaluate")
    async def evaluate(request: EvaluateRequest):
        """取得して審査・価格計算: URL + manual cost → legal screening → JPY price → saved as a draft (not public)."""
        if not request.product.strip():
            return _error(ali.AlibabaError("1688 の商品 URL（/offer/<数字>.html）か商品 ID を入れてください。", 422, "invalid_product"))
        try:
            return _json(await run_in_threadpool(create_draft, request))
        except ali.AlibabaError as exc:
            return _error(exc)

    @router.get("/drafts")
    def drafts():
        return _json({"drafts": stores().drafts()})

    @router.delete("/drafts/{draft_id}")
    def delete_draft(draft_id: str):
        return _json({"removed": stores().delete_draft(draft_id)})

    def publish_draft(draft_id: str, via: str = "admin") -> dict[str, Any]:
        """Publish a saved draft, only when SAFE, priced and titled. Shared by /publish and chat auto-publish."""
        def work():
            store = stores()
            draft = store.draft(draft_id)
            if draft is None:
                raise ali.AlibabaError("下書きが見つかりません。もう一度「取得して審査・価格計算」をしてください。", 404, "draft_not_found")
            product, review_result, pricing = draft["product"], draft["review"], draft["pricing"]
            if review_result["verdict"] != "SAFE":
                return draft, None, "法的審査で REJECT のため登録できません。"
            if not pricing:
                return draft, None, "原価（元）を入力して計算し直してください。"
            if product.get("title_missing"):
                return draft, None, "商品名を入力して計算し直してください。"
            listing = store.publish(shop_store.listing_from(product, review_result, pricing))
            store.delete_draft(draft_id)
            return draft, listing, None
        try:
            draft, listing, reason = work()
        except ali.AlibabaError as exc:
            log.add(action="publish", status="error", code=exc.code, message=exc.message, via=via)
            raise
        review_result = draft["review"]
        entry = log.add(action="publish", status="ok" if listing else "rejected", **summary(draft["product"]),
                        verdict=review_result["verdict"], engine=review_result["engine"], checks=checks(review_result),
                        selling_price_jpy=(draft["pricing"] or {}).get("selling_price_jpy"), shop_id=(listing or {}).get("id"),
                        message=reason or ("SAFE（自動掲載完了）" if via == "chat" else None), via=via)
        return {"published": bool(listing), "listing": shop_store.public_product(listing) if listing else None,
                "reason": reason, "draft": draft, "log": entry}

    router.publish_draft = publish_draft

    @router.post("/publish")
    async def publish(request: PublishRequest):
        """ショップに登録: publish a saved draft, only when SAFE, priced and titled."""
        try:
            body = await run_in_threadpool(publish_draft, request.draft_id)
        except ali.AlibabaError as exc:
            return _error(exc)
        return _json(body, 201 if body["published"] else 409)

    return router
