"""Owner-authenticated JSON API: 1688 Open API fetch → legal screening → JPY pricing."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import alibaba1688 as ali


class PricingOverrides(BaseModel):
    cny_jpy_rate: float | None = None
    agent_fee_rate: float | None = None
    shipping_jpy: float | None = None
    duty_rate: float | None = None
    platform_fee_rate: float | None = None
    target_margin_rate: float | None = None


class FetchRequest(BaseModel):
    product: str = Field(min_length=1, max_length=500, description="1688 商品 URL または商品 ID")


class EvaluateRequest(FetchRequest):
    pricing: PricingOverrides = Field(default_factory=PricingOverrides)


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def _error(exc: ali.AlibabaError) -> JSONResponse:
    return _json({"detail": exc.message, "code": exc.code}, exc.status_code)


def build_router(auth: Callable[..., Any], client_factory: Callable[[], Any] | None = None,
                 reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/alibaba", tags=["alibaba"], dependencies=[Depends(auth)])
    clients = client_factory or ali.client_from_env
    review = reviewer or ali.review_product

    @router.get("/config")
    def config():
        try:
            clients()
            configured, reason = True, None
        except ali.AlibabaError as exc:
            configured, reason = False, exc.message
        return _json({"configured": configured, "reason": reason, "api": "alibaba.cross.productInfo",
                      "pricing": ali.pricing_defaults(), "checks": ali.CHECK_LABELS})

    def fetch(product: str) -> dict[str, Any]:
        return clients().product(ali.parse_product_id(product))

    @router.post("/fetch")
    async def fetch_product(request: FetchRequest):
        try:
            return _json({"product": await run_in_threadpool(fetch, request.product)})
        except ali.AlibabaError as exc:
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
            return _json(await run_in_threadpool(work))
        except ali.AlibabaError as exc:
            return _error(exc)

    return router
