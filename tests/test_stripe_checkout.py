"""Stripe REST client: key handling, error mapping, Checkout payload and order extraction."""
import hashlib
import hmac
import time

import pytest
import requests

from karry_agent import stripe_checkout as sc

BASE = "https://shop.example.com"
PRODUCT = {"id": "p1", "title": "T" * 300, "price_jpy": 1980, "images": []}


@pytest.mark.parametrize("key,configured,mode", [
    ("sk_test_abc", True, "test"),
    ("  sk_live_abc\n", True, "live"),
    ("rk_live_abc", True, "live"),
    ("rk_test_abc", True, "test"),
    ("pk_live_abc", False, None),
    ("", False, None),
])
def test_secret_key_prefixes_decide_configuration_and_mode(monkeypatch, key, configured, mode):
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    assert sc.configured() is configured
    if mode:
        assert sc.mode() == mode


class FakeResponse:
    def __init__(self, status, body=None, error=None):
        self.status_code, self._body, self._error = status, body, error

    def json(self):
        if self._error:
            raise self._error
        return self._body


def _respond(monkeypatch, response=None, exc=None):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if exc:
            raise exc
        return response

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_secret")
    monkeypatch.setattr(sc.requests, "request", fake)
    return calls


def test_request_sends_basic_auth_and_a_timeout(monkeypatch):
    calls = _respond(monkeypatch, FakeResponse(200, {"id": "cs_test_1"}))
    assert sc.retrieve_session("cs_test_1") == {"id": "cs_test_1"}
    method, url, kwargs = calls[0]
    assert (method, url) == ("GET", "https://api.stripe.com/v1/checkout/sessions/cs_test_1")
    assert kwargs["auth"] == ("sk_test_secret", "") and kwargs["timeout"] == sc.TIMEOUT


@pytest.mark.parametrize("response,exc,expected", [
    (None, requests.ConnectionError("down"), "接続できませんでした"),
    (FakeResponse(502, error=ValueError("not json")), None, "接続できませんでした"),
    (FakeResponse(402, {"error": {"message": "Your card was declined."}}), None, "（402）: Your card was declined."),
    (FakeResponse(500, ["unexpected"]), None, "（500）: 詳細不明"),
    (FakeResponse(400, {"error": None}), None, "（400）: 詳細不明"),
])
def test_request_failures_become_stripe_errors(monkeypatch, response, exc, expected):
    _respond(monkeypatch, response, exc)
    with pytest.raises(sc.StripeError, match=expected) as raised:
        sc.retrieve_session("cs_test_1")
    assert "sk_test_secret" not in str(raised.value)


def _captured_session(monkeypatch, product, quantity=1, variant=""):
    sent = {}
    monkeypatch.setattr(sc, "_request", lambda method, path, data=None: sent.update(method=method, path=path, data=data) or {"id": "cs"})
    sc.create_session(product, quantity, BASE, variant)
    assert (sent["method"], sent["path"]) == ("POST", "/checkout/sessions")
    return sent["data"]


def test_checkout_session_uses_server_side_price_and_return_urls(monkeypatch):
    data = _captured_session(monkeypatch, {**PRODUCT, "price_jpy": "1980"}, quantity=3)
    assert data["line_items[0][price_data][unit_amount]"] == 1980
    assert data["line_items[0][price_data][currency]"] == "jpy"
    assert data["line_items[0][quantity]"] == 3 and data["metadata[quantity]"] == 3
    assert len(data["line_items[0][price_data][product_data][name]"]) == 250
    assert len(data["metadata[title]"]) == 200
    assert data["success_url"] == f"{BASE}/shop?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    assert data["cancel_url"] == f"{BASE}/products/p1?checkout=cancel"
    assert "metadata[variant]" not in data
    assert not any("images" in key for key in data)


@pytest.mark.parametrize("images,expected", [
    (["http://insecure.example/a.jpg", "/etc/passwd", "javascript:alert(1)", "/api/shop/images/abc.jpg"], BASE + "/api/shop/images/abc.jpg"),
    (["https://cdn.example/a.jpg", "/api/shop/images/abc.jpg"], "https://cdn.example/a.jpg"),
    (["data:image/png;base64,AAAA", "//evil.example/a.jpg"], None),
])
def test_only_https_or_own_shop_images_are_sent_to_stripe(monkeypatch, images, expected):
    data = _captured_session(monkeypatch, {**PRODUCT, "images": images})
    assert data.get("line_items[0][price_data][product_data][images][0]") == expected


def test_variant_is_recorded_and_truncated(monkeypatch):
    data = _captured_session(monkeypatch, PRODUCT, variant="V" * 300)
    assert data["metadata[variant]"] == "V" * 200
    assert data["line_items[0][price_data][product_data][description]"] == "V" * 200


def test_order_reads_shipping_from_newer_collected_information_shape():
    session = {
        "id": "cs_live_1", "livemode": True, "amount_total": 3960,
        "metadata": {"quantity": "2", "variant": "Red"},
        "customer_details": {"email": "buyer@example.com", "phone": "+8190", "name": "Billing Name"},
        "collected_information": {"shipping_details": {"name": "Ship Name", "address": {
            "postal_code": "100-0001", "state": "東京都", "city": "千代田区", "line1": "1-1", "line2": None, "country": "JP"}}},
    }
    order = sc.order_from_session(session, PRODUCT)
    assert order["name"] == "Ship Name" and order["quantity"] == 2 and order["variant"] == "Red"
    assert order["total_jpy"] == 3960 and order["unit_price_jpy"] == 1980
    assert order["stripe_mode"] == "live" and order["status"] == "paid"
    assert order["contact"] == "buyer@example.com"
    assert order["address"] == {"postal_code": "100-0001", "state": "東京都", "city": "千代田区", "line1": "1-1", "line2": "", "country": "JP"}


def test_order_falls_back_to_customer_details_and_safe_defaults():
    session = {"id": "cs_test_2", "customer_details": {"phone": "+8190", "name": "Buyer", "address": {"city": "Osaka"}}}
    order = sc.order_from_session(session, PRODUCT)
    assert order["name"] == "Buyer" and order["contact"] == "+8190" and order["email"] == ""
    assert order["quantity"] == 1 and order["total_jpy"] == 0 and order["variant"] == ""
    assert order["address"]["city"] == "Osaka" and order["address"]["line1"] == ""
    assert order["stripe_mode"] == "test"


def _sign(payload: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()


def test_webhook_signature_accepts_any_matching_v1_within_tolerance():
    now, payload, secret = 1_800_000_000, b'{"id":"evt"}', "whsec_test"
    good = _sign(payload, secret, now)
    assert sc.verify_webhook(payload, f"t={now},v1={'0' * 64},v1={good}", secret, now=now)
    assert sc.verify_webhook(payload, f"t={now},v1={good}", secret, now=now + sc.SIGNATURE_TOLERANCE)
    assert not sc.verify_webhook(payload, f"t={now},v1={good}", secret, now=now + sc.SIGNATURE_TOLERANCE + 1)
    assert not sc.verify_webhook(payload, f"t={now},v1={good}", secret, now=now - sc.SIGNATURE_TOLERANCE - 1)


def test_webhook_signature_rejects_tampering_and_other_schemes():
    now, payload, secret = int(time.time()), b'{"id":"evt"}', "whsec_test"
    good = _sign(payload, secret, now)
    assert not sc.verify_webhook(payload + b" ", f"t={now},v1={good}", secret)
    assert not sc.verify_webhook(payload, f"t={now + 1},v1={good}", secret)
    assert not sc.verify_webhook(payload, f"t={now},v0={good}", secret)
    assert not sc.verify_webhook(payload, f"v1={good}", secret)
    assert not sc.verify_webhook(payload, f"t={now},v1={good}", "whsec_other")
