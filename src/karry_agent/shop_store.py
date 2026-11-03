"""KERRY shop catalog and order inbox, kept in the private task bucket (memory when no bucket is configured)."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Any

from . import task_persistence

MAX_PRODUCTS = 500
MAX_ORDERS = 1000
_MEMORY: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


class ShopStore:
    def __init__(self):
        self.durable = task_persistence.mode() == "s3"
        self.repo = task_persistence.repository() if self.durable else None
        self.prefix = ("" if self.repo is None else self.repo.prefix) + "shop/"

    def _read(self, name: str) -> dict[str, Any]:
        if not self.durable:
            with _LOCK:
                return copy.deepcopy(_MEMORY.get(self.prefix + name, {}))
        value, _ = self.repo._read(self.prefix + name)
        return value or {}

    def _update(self, name: str, apply) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def wrapped(current: dict[str, Any]) -> dict[str, Any]:
            result["value"] = apply(current)
            return current
        if not self.durable:
            with _LOCK:
                current = copy.deepcopy(_MEMORY.get(self.prefix + name, {"version": 1}))
                wrapped(current)
                _MEMORY[self.prefix + name] = current
        else:
            self.repo._cas(self.prefix + name, {"version": 1}, wrapped)
        return result.get("value")

    def products(self) -> list[dict[str, Any]]:
        items = self._read("catalog.json").get("products", {})
        return sorted(items.values(), key=lambda p: p["created_at"], reverse=True)

    def publish(self, listing: dict[str, Any]) -> dict[str, Any]:
        """Insert or refresh one listing; the same 1688 product ID keeps its shop ID."""
        def apply(current):
            items = current.setdefault("products", {})
            existing = next((p for p in items.values() if p.get("source_product_id") == listing["source_product_id"]), None)
            now = time.time()
            record = {**listing, "id": existing["id"] if existing else uuid.uuid4().hex,
                      "created_at": existing["created_at"] if existing else now, "updated_at": now, "status": "published"}
            items[record["id"]] = record
            for old in sorted(items.values(), key=lambda p: p["created_at"])[:-MAX_PRODUCTS]:
                items.pop(old["id"], None)
            return record
        return self._update("catalog.json", apply)

    def unpublish(self, product_id: str) -> bool:
        return bool(self._update("catalog.json", lambda current: current.setdefault("products", {}).pop(product_id, None)))

    def orders(self) -> list[dict[str, Any]]:
        return sorted(self._read("orders.json").get("orders", {}).values(), key=lambda o: o["created_at"], reverse=True)

    def add_order(self, order: dict[str, Any]) -> dict[str, Any]:
        def apply(current):
            orders = current.setdefault("orders", {})
            record = {**order, "id": uuid.uuid4().hex, "created_at": time.time(), "status": "received"}
            orders[record["id"]] = record
            for old in sorted(orders.values(), key=lambda o: o["created_at"])[:-MAX_ORDERS]:
                orders.pop(old["id"], None)
            return record
        return self._update("orders.json", apply)


def listing_from(product: dict[str, Any], review: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Any]:
    specs = [a for a in product.get("attributes") or [] if a.get("name") and a.get("value")][:30]
    return {"source": "1688", "source_product_id": product["product_id"], "title": product.get("title") or "1688 商品",
            "category": product.get("category") or "その他", "images": (product.get("images") or [])[:12], "specs": specs,
            "description": product.get("description_excerpt") or "", "price_jpy": int(pricing["selling_price_jpy"]),
            "variants": [", ".join(f"{a['name']}: {a['value']}" for a in s.get("attributes") or []) for s in product.get("skus") or []][:30],
            "review": {"verdict": review["verdict"], "engine": review["engine"]}}


def public_product(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "category", "images", "specs", "description", "price_jpy", "variants", "created_at")
    return {k: item.get(k) for k in keys}
