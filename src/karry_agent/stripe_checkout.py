"""Stripe Checkout for the KERRY shop over the REST API (no SDK); keys come from STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import requests

API = "https://api.stripe.com/v1"
TIMEOUT = 20
SIGNATURE_TOLERANCE = 300


class StripeError(RuntimeError):
    pass


def secret_key() -> str:
    return os.getenv("STRIPE_SECRET_KEY", "").strip()


def publishable_key() -> str:
    return os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()


def configured() -> bool:
    return secret_key().startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_"))


def mode() -> str:
    return "live" if "_live_" in secret_key()[:8] else "test"


def _request(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = requests.request(method, API + path, data=data, auth=(secret_key(), ""), timeout=TIMEOUT)
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise StripeError("Stripe に接続できませんでした。") from exc
    if response.status_code >= 400:
        message = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        raise StripeError(f"Stripe がエラーを返しました（{response.status_code}）: {message or '詳細不明'}")
    return body


def _absolute(base_url: str, src: str) -> str | None:
    if src.startswith("https://"):
        return src
    if src.startswith("/api/shop/images/"):
        return base_url + src
    return None


def create_session(product: dict[str, Any], quantity: int, base_url: str, variant: str = "") -> dict[str, Any]:
    data: dict[str, Any] = {
        "mode": "payment", "locale": "ja",
        "line_items[0][quantity]": quantity,
        "line_items[0][price_data][currency]": "jpy",
        "line_items[0][price_data][unit_amount]": int(product["price_jpy"]),
        "line_items[0][price_data][product_data][name]": product["title"][:250],
        "shipping_address_collection[allowed_countries][0]": "JP",
        "phone_number_collection[enabled]": "true",
        "metadata[product_id]": product["id"], "metadata[quantity]": quantity, "metadata[title]": product["title"][:200],
        "success_url": f"{base_url}/shop?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/products/{product['id']}?checkout=cancel",
    }
    if variant:
        data["metadata[variant]"] = variant[:200]
        data["line_items[0][price_data][product_data][description]"] = variant[:200]
    image = next((u for u in (_absolute(base_url, s) for s in product.get("images") or []) if u), None)
    if image:
        data["line_items[0][price_data][product_data][images][0]"] = image
    return _request("POST", "/checkout/sessions", data)


def retrieve_session(session_id: str) -> dict[str, Any]:
    return _request("GET", f"/checkout/sessions/{session_id}")


def order_from_session(session: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
    """Buyer + shipping details as Stripe collected them (newer API versions nest them under collected_information)."""
    customer = session.get("customer_details") or {}
    shipping = session.get("shipping_details") or (session.get("collected_information") or {}).get("shipping_details") or {}
    address = shipping.get("address") or customer.get("address") or {}
    metadata = session.get("metadata") or {}
    quantity = int(metadata.get("quantity") or 1)
    return {
        "product_id": product["id"], "title": product["title"], "quantity": quantity, "variant": metadata.get("variant") or "",
        "unit_price_jpy": int(product["price_jpy"]), "total_jpy": int(session.get("amount_total") or 0),
        "name": shipping.get("name") or customer.get("name") or "", "phone": customer.get("phone") or "",
        "email": customer.get("email") or "", "contact": customer.get("email") or customer.get("phone") or "",
        "address": {k: address.get(k) or "" for k in ("postal_code", "state", "city", "line1", "line2", "country")},
        "payment": "stripe", "stripe_session_id": session["id"], "stripe_mode": "live" if session.get("livemode") else "test",
        "status": "paid",
    }


def verify_webhook(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    pairs = [p.strip().split("=", 1) for p in header.split(",") if "=" in p]
    signatures = [v for k, v in pairs if k == "v1"]
    try:
        timestamp = int(dict(pairs).get("t", ""))
    except ValueError:
        return False
    if abs((now or time.time()) - timestamp) > SIGNATURE_TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)
