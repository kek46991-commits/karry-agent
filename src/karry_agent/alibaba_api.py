"""Owner-authenticated JSON API: 1688 Open API fetch → legal screening → JPY pricing."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import alibaba1688 as ali
from . import shop_store


class PricingOverrides(BaseModel):
    cny_jpy_rate: float | None = None
    agent_fee_rate: float | None = None
    shipping_jpy: float | None = None
    markup: float | None = None


class FetchRequest(BaseModel):
    product: str = Field(min_length=1, max_length=500, description="1688 商品 URL または商品 ID")


class EvaluateRequest(FetchRequest):
    pricing: PricingOverrides = Field(default_factory=PricingOverrides)


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


def build_router(auth: Callable[..., Any], client_factory: Callable[[], Any] | None = None,
                 reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 audit: AuditLog | None = None, store_factory: Callable[[], Any] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/alibaba", tags=["alibaba"], dependencies=[Depends(auth)])
    clients = client_factory or ali.client_from_env
    review = reviewer or ali.review_product
    log = audit or AuditLog()
    stores = store_factory or shop_store.ShopStore
    router.audit = log

    def safe_id(product: str) -> str | None:
        try:
            return ali.parse_product_id(product)
        except ali.AlibabaError:
            return None

    @router.get("/config")
    def config():
        try:
            clients()
            configured, reason = True, None
        except ali.AlibabaError as exc:
            configured, reason = False, exc.message
        return _json({"configured": configured, "reason": reason, "api": "alibaba.cross.productInfo",
                      "missing": [] if configured else ali.missing_env(),
                      "pricing": ali.pricing_defaults(), "checks": ali.CHECK_LABELS})

    @router.get("/logs")
    def logs(limit: int = 50):
        return _json({"entries": log.list(max(1, min(limit, MAX_LOG_ENTRIES)))})

    def fetch(product: str) -> dict[str, Any]:
        return clients().product(ali.parse_product_id(product))

    @router.post("/fetch")
    async def fetch_product(request: FetchRequest):
        try:
            product = await run_in_threadpool(fetch, request.product)
            log.add(action="fetch", status="ok", product_id=product.get("product_id"), title=product.get("title"),
                    images=len(product.get("images") or []))
            return _json({"product": product})
        except ali.AlibabaError as exc:
            log.add(action="fetch", status="error", product_id=safe_id(request.product), code=exc.code, message=exc.message)
            return _error(exc)

    @router.post("/evaluate")
    async def evaluate(request: EvaluateRequest):
        def work():
            product = fetch(request.product)
            result = {"product": product, "review": review(product), "pricing": None, "pricing_error": None}
            try:
                result["pricing"] = ali.price_product(product.get("min_price_cny"), request.pricing.model_dump(exclude_none=True))
            except ali.AlibabaError as exc:
                if exc.code == "invalid_pricing":
                    raise
                result["pricing_error"] = exc.message
            return result
        try:
            result = await run_in_threadpool(work)
        except ali.AlibabaError as exc:
            log.add(action="evaluate", status="error", product_id=safe_id(request.product), code=exc.code, message=exc.message)
            return _error(exc)
        product, review_result = result["product"], result["review"]
        result["log"] = log.add(action="evaluate", status="ok", product_id=product.get("product_id"), title=product.get("title"),
                                images=len(product.get("images") or []), verdict=review_result["verdict"],
                                engine=review_result["engine"],
                                checks={k: {"status": v["status"], "reason": v["reason"]} for k, v in review_result["checks"].items()},
                                selling_price_jpy=(result["pricing"] or {}).get("selling_price_jpy"))
        return _json(result)

    @router.post("/publish")
    async def publish(request: EvaluateRequest):
        """本番仕入れ＆自動出品: live 1688 fetch → legal screening → KERRY shop listing (SAFE + priced only)."""
        def work():
            product = fetch(request.product)
            review_result = review(product)
            pricing = ali.price_product(product.get("min_price_cny"), request.pricing.model_dump(exclude_none=True)) \
                if review_result["verdict"] == "SAFE" else None
            listing = stores().publish(shop_store.listing_from(product, review_result, pricing)) if pricing else None
            return product, review_result, pricing, listing
        try:
            product, review_result, pricing, listing = await run_in_threadpool(work)
        except ali.AlibabaError as exc:
            log.add(action="publish", status="error", product_id=safe_id(request.product), code=exc.code, message=exc.message)
            return _error(exc)
        checks = {k: {"status": v["status"], "reason": v["reason"]} for k, v in review_result["checks"].items()}
        entry = log.add(action="publish", status="ok" if listing else "rejected", product_id=product.get("product_id"),
                        title=product.get("title"), images=len(product.get("images") or []), verdict=review_result["verdict"],
                        engine=review_result["engine"], checks=checks, selling_price_jpy=(pricing or {}).get("selling_price_jpy"),
                        shop_id=(listing or {}).get("id"))
        body = {"published": bool(listing), "listing": shop_store.public_product(listing) if listing else None,
                "product": product, "review": review_result, "pricing": pricing, "log": entry}
        return _json(body, 201 if listing else 409)

    return router
