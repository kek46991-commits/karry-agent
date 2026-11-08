"""KERRY shop catalog and order inbox, kept in the private task bucket (memory when no bucket is configured)."""
from __future__ import annotations

import base64
import copy
import hashlib
import threading
import time
import uuid
from typing import Any

from . import task_persistence

MAX_PRODUCTS = 500
MAX_ORDERS = 1000
MAX_DRAFTS = 200
MAX_MEMORY_IMAGES = 300
IMAGE_URL = "/api/shop/images/{}.jpg"
_MEMORY: dict[str, dict[str, Any]] = {}
_IMAGES: dict[str, bytes] = {}
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

    def save_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Admin-only draft (product + review + pricing); the same 1688 product ID keeps its draft ID."""
        def apply(current):
            items = current.setdefault("drafts", {})
            existing = next((d for d in items.values() if d["product"]["product_id"] == draft["product"]["product_id"]), None)
            record = {**draft, "id": existing["id"] if existing else uuid.uuid4().hex, "updated_at": time.time()}
            items[record["id"]] = record
            for old in sorted(items.values(), key=lambda d: d["updated_at"])[:-MAX_DRAFTS]:
                items.pop(old["id"], None)
            return record
        return self._update("drafts.json", apply)

    def drafts(self) -> list[dict[str, Any]]:
        return sorted(self._read("drafts.json").get("drafts", {}).values(), key=lambda d: d["updated_at"], reverse=True)

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        return self._read("drafts.json").get("drafts", {}).get(draft_id)

    def delete_draft(self, draft_id: str) -> bool:
        return bool(self._update("drafts.json", lambda current: current.setdefault("drafts", {}).pop(draft_id, None)))

    def save_image(self, data: bytes) -> str:
        """Store a JPEG (content-addressed) and return its public shop URL."""
        image_id = hashlib.sha256(data).hexdigest()[:32]
        if not self.durable:
            with _LOCK:
                _IMAGES[image_id] = data
                for old in list(_IMAGES)[:-MAX_MEMORY_IMAGES]:
                    _IMAGES.pop(old, None)
        else:
            encoded = base64.b64encode(data).decode()
            self.repo._cas(self.prefix + f"images/{image_id}.json", {"version": 1}, lambda current: {**current, "jpeg": encoded})
        return IMAGE_URL.format(image_id)

    def image(self, image_id: str) -> bytes | None:
        if not self.durable:
            with _LOCK:
                return _IMAGES.get(image_id)
        value, _ = self.repo._read(self.prefix + f"images/{image_id}.json")
        return base64.b64decode(value["jpeg"]) if value and isinstance(value.get("jpeg"), str) else None

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
    variants = product.get("variants") or [", ".join(f"{a['name']}: {a['value']}" for a in s.get("attributes") or [])
                                            for s in product.get("skus") or []]
    return {"source": "1688", "source_product_id": product["product_id"], "title": product.get("title") or "1688 商品",
            "category": product.get("category") or "その他", "images": (product.get("images") or [])[:12], "specs": specs,
            "description": product.get("description_excerpt") or "", "price_jpy": int(pricing["selling_price_jpy"]),
            "variants": [v for v in variants if v][:30], "source_url": product.get("url"),
            "review": {"verdict": review["verdict"], "engine": review["engine"]}}


def published_product(store: Any, product_id: str) -> dict[str, Any] | None:
    if len(product_id) != 32 or any(c not in "0123456789abcdef" for c in product_id):
        return None
    return next((p for p in store.products() if p["id"] == product_id and p.get("status") == "published"), None)


def public_product(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "category", "images", "specs", "description", "price_jpy", "variants", "created_at")
    return {k: item.get(k) for k in keys}
