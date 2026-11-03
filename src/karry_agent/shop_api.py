"""Public KERRY shop API (catalog + order requests) and owner-only order/listing management."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import shop_store


class OrderRequest(BaseModel):
    product_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    quantity: int = Field(default=1, ge=1, le=99)
    name: str = Field(min_length=1, max_length=80)
    contact: str = Field(min_length=3, max_length=200, description="メールアドレスまたは電話番号")
    note: str = Field(default="", max_length=500)


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def build_routers(auth: Callable[..., Any], store_factory: Callable[[], Any] | None = None) -> tuple[APIRouter, APIRouter]:
    stores = store_factory or shop_store.ShopStore
    public = APIRouter(prefix="/api/shop", tags=["shop"])
    owner = APIRouter(prefix="/api/shop/admin", tags=["shop"], dependencies=[Depends(auth)])

    @public.get("/products")
    def products():
        items = [shop_store.public_product(p) for p in stores().products() if p.get("status") == "published"]
        return _json({"products": items, "categories": sorted({p["category"] for p in items})})

    @public.post("/orders")
    def order(request: OrderRequest):
        store = stores()
        product = next((p for p in store.products() if p["id"] == request.product_id and p.get("status") == "published"), None)
        if product is None:
            return _json({"detail": "この商品は現在販売していません。", "code": "product_unavailable"}, 404)
        record = store.add_order({**request.model_dump(), "title": product["title"], "unit_price_jpy": product["price_jpy"],
                                  "total_jpy": product["price_jpy"] * request.quantity})
        return _json({"order_id": record["id"], "status": record["status"], "total_jpy": record["total_jpy"]}, 201)

    @owner.get("/orders")
    def orders():
        return _json({"orders": stores().orders()})

    @owner.get("/products")
    def admin_products():
        return _json({"products": [{**shop_store.public_product(p), "source_url": p.get("source_url"), "review": p.get("review")}
                                   for p in stores().products()]})

    @owner.delete("/products/{product_id}")
    def unpublish(product_id: str):
        return _json({"removed": stores().unpublish(product_id)})

    return public, owner
