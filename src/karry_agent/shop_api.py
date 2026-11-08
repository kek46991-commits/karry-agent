"""Public KERRY shop API (catalog, Stripe Checkout, order requests) and owner-only order/listing management."""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import shop_store, stripe_checkout

log = logging.getLogger(__name__)
STRIPE_MIN_JPY = 50


class OrderRequest(BaseModel):
    product_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    quantity: int = Field(default=1, ge=1, le=99)
    name: str = Field(min_length=1, max_length=80)
    contact: str = Field(min_length=3, max_length=200, description="メールアドレスまたは電話番号")
    note: str = Field(default="", max_length=500)


class CheckoutRequest(BaseModel):
    product_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    quantity: int = Field(default=1, ge=1, le=99)
    variant: str = Field(default="", max_length=200)


class ConfirmRequest(BaseModel):
    session_id: str = Field(pattern=r"^cs_(test|live)_[A-Za-z0-9]{10,200}$")


class ListPriceRequest(BaseModel):
    list_price_jpy: int | None = Field(default=None, ge=1, le=10_000_000)


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def _base_url() -> str:
    from .instant_website import public_base_url
    return public_base_url()


def build_routers(auth: Callable[..., Any], store_factory: Callable[[], Any] | None = None) -> tuple[APIRouter, APIRouter]:
    stores = store_factory or shop_store.ShopStore
    public = APIRouter(prefix="/api/shop", tags=["shop"])
    owner = APIRouter(prefix="/api/shop/admin", tags=["shop"], dependencies=[Depends(auth)])
    unavailable = {"detail": "この商品は現在販売していません。", "code": "product_unavailable"}
    not_ready = {"detail": "カード決済はまだ準備中です。", "code": "stripe_not_configured"}

    def record_session(session: dict[str, Any]) -> JSONResponse:
        if session.get("payment_status") != "paid":
            return _json({"detail": "お支払いはまだ完了していません。", "code": "payment_incomplete"}, 409)
        store, metadata = stores(), session.get("metadata") or {}
        product = next((p for p in store.products() if p["id"] == metadata.get("product_id")), None)
        if product is None:
            quantity = max(1, int(metadata.get("quantity") or 1))
            product = {"id": metadata.get("product_id") or "", "title": metadata.get("title") or "(掲載終了の商品)",
                       "price_jpy": int(session.get("amount_total") or 0) // quantity}
        record = store.record_paid_order(stripe_checkout.order_from_session(session, product))
        return _json({"order_id": record["id"], "status": record["status"], "title": record["title"],
                      "quantity": record["quantity"], "total_jpy": record["total_jpy"]})

    @public.get("/products")
    def products():
        store = stores()
        sold = store.sold_counts()
        items = [shop_store.public_product(p, sold.get(p["id"], 0)) for p in store.products() if p.get("status") == "published"]
        return _json({"products": items, "categories": sorted({p["category"] for p in items})})

    @public.get("/products/{product_id}")
    def product(product_id: str):
        store = stores()
        item = shop_store.published_product(store, product_id)
        if item is None:
            return _json(unavailable, 404)
        return _json({"product": shop_store.public_product(item, store.sold_counts().get(item["id"], 0))})

    @public.get("/config")
    def config():
        ready = stripe_checkout.configured()
        return _json({"checkout": "stripe" if ready else "request", "stripe_mode": stripe_checkout.mode() if ready else None,
                      "publishable_key": stripe_checkout.publishable_key() or None if ready else None})

    @public.get("/images/{image_id}.jpg")
    def image(image_id: str):
        data = stores().image(image_id) if len(image_id) == 32 and all(c in "0123456789abcdef" for c in image_id) else None
        if data is None:
            return _json({"detail": "not found"}, 404)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400",
                                                                 "X-Content-Type-Options": "nosniff"})

    @public.post("/checkout")
    def checkout(request: CheckoutRequest):
        if not stripe_checkout.configured():
            return _json(not_ready, 503)
        item = shop_store.published_product(stores(), request.product_id)
        if item is None:
            return _json(unavailable, 404)
        if request.variant and request.variant not in (item.get("variants") or []):
            return _json({"detail": "選んだバリエーションが見つかりません。", "code": "variant_unavailable"}, 422)
        if int(item.get("price_jpy") or 0) < STRIPE_MIN_JPY:
            return _json({"detail": "この商品はカード決済できません。", "code": "price_unavailable"}, 422)
        try:
            session = stripe_checkout.create_session(item, request.quantity, _base_url(), request.variant)
        except stripe_checkout.StripeError as exc:
            log.warning("Stripe checkout session failed: %s", exc)
            return _json({"detail": "決済ページを作れませんでした。時間をおいてもう一度お試しください。", "code": "stripe_error"}, 502)
        return _json({"url": session["url"], "session_id": session["id"]}, 201)

    @public.post("/checkout/confirm")
    def confirm(request: ConfirmRequest):
        if not stripe_checkout.configured():
            return _json(not_ready, 503)
        try:
            session = stripe_checkout.retrieve_session(request.session_id)
        except stripe_checkout.StripeError as exc:
            log.warning("Stripe session lookup failed: %s", exc)
            return _json({"detail": "お支払いを確認できませんでした。", "code": "stripe_error"}, 502)
        return record_session(session)

    @public.post("/stripe/webhook")
    async def webhook(request: Request):
        secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
        if not secret:
            return _json({"detail": "webhook not configured"}, 503)
        payload = await request.body()
        if not stripe_checkout.verify_webhook(payload, request.headers.get("stripe-signature", ""), secret):
            return _json({"detail": "invalid signature"}, 400)
        try:
            event = json.loads(payload)
        except ValueError:
            return _json({"detail": "invalid payload"}, 400)
        if event.get("type") in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            session = (event.get("data") or {}).get("object") or {}
            if session.get("payment_status") == "paid" and str(session.get("id", "")).startswith("cs_"):
                record_session(session)
        return _json({"received": True})

    @public.post("/orders")
    def order(request: OrderRequest):
        store = stores()
        product = next((p for p in store.products() if p["id"] == request.product_id and p.get("status") == "published"), None)
        if product is None:
            return _json(unavailable, 404)
        record = store.add_order({**request.model_dump(), "title": product["title"], "unit_price_jpy": product["price_jpy"],
                                  "total_jpy": product["price_jpy"] * request.quantity})
        return _json({"order_id": record["id"], "status": record["status"], "total_jpy": record["total_jpy"]}, 201)

    @owner.get("/orders")
    def orders():
        return _json({"orders": stores().orders()})

    @owner.get("/products")
    def admin_products():
        store = stores()
        sold = store.sold_counts()
        return _json({"products": [{**shop_store.public_product(p, sold.get(p["id"], 0)), "sold": sold.get(p["id"], 0),
                                    "source_url": p.get("source_url"), "review": p.get("review")} for p in store.products()]})

    @owner.patch("/products/{product_id}")
    def set_list_price(product_id: str, request: ListPriceRequest):
        store = stores()
        item = next((p for p in store.products() if p["id"] == product_id), None)
        if item is None:
            return _json({"detail": "商品が見つかりません。"}, 404)
        if request.list_price_jpy is not None and request.list_price_jpy <= int(item.get("price_jpy") or 0):
            return _json({"detail": "通常価格は販売価格より高い金額にしてください。", "code": "list_price_too_low"}, 422)
        return _json({"product": shop_store.public_product(store.set_list_price(product_id, request.list_price_jpy))})

    @owner.delete("/products/{product_id}")
    def unpublish(product_id: str):
        return _json({"removed": stores().unpublish(product_id)})

    return public, owner
