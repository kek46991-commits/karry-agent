import hashlib
import hmac
import json
import time

import pytest

from karry_agent import shop_store, stripe_checkout
from tests.test_kerry_shop import AUTH, register, shop  # noqa: F401

SESSION = 'cs_test_' + 'a1B2c3D4e5' * 3


@pytest.fixture
def stripe(monkeypatch, shop):  # noqa: F811
    monkeypatch.setenv('STRIPE_SECRET_KEY', 'sk_test_' + 'x' * 24)
    monkeypatch.setenv('STRIPE_PUBLISHABLE_KEY', 'pk_test_' + 'y' * 24)
    monkeypatch.setenv('RENDER_EXTERNAL_URL', 'https://shop.example')
    calls = []
    sessions = {}

    def fake(method, path, data=None):
        calls.append((method, path, data))
        if method == 'POST':
            sessions[SESSION] = {'id': SESSION, 'url': 'https://checkout.stripe.com/c/pay/' + SESSION, 'payment_status': 'unpaid',
                                 'amount_total': data['line_items[0][price_data][unit_amount]'] * data['line_items[0][quantity]'],
                                 'livemode': False, 'metadata': {'product_id': data['metadata[product_id]'],
                                                                 'quantity': str(data['metadata[quantity]']), 'title': data['metadata[title]']}}
            return sessions[SESSION]
        return sessions[path.rsplit('/', 1)[1]]
    monkeypatch.setattr(stripe_checkout, '_request', fake)
    http, _ = shop
    return http, calls, sessions


def pay(session):
    session.update(payment_status='paid', customer_details={'name': '山田 太郎', 'email': 'taro@example.com', 'phone': '+819012345678'},
                   collected_information={'shipping_details': {'name': '山田 太郎', 'address': {
                       'postal_code': '150-0001', 'state': '東京都', 'city': '渋谷区', 'line1': '神宮前1-2-3', 'line2': '101', 'country': 'JP'}}})


def test_config_reports_request_mode_without_keys(shop, monkeypatch):  # noqa: F811
    monkeypatch.delenv('STRIPE_SECRET_KEY', raising=False)
    http, _ = shop
    assert http.get('/api/shop/config').json() == {'checkout': 'request', 'stripe_mode': None, 'publishable_key': None}
    listing = register(http, '573741401425').json()['listing']
    response = http.post('/api/shop/checkout', json={'product_id': listing['id']})
    assert response.status_code == 503 and response.json()['code'] == 'stripe_not_configured'
    assert http.post('/api/shop/checkout/confirm', json={'session_id': SESSION}).status_code == 503


def test_checkout_uses_server_price_and_records_paid_order_once(stripe):
    http, calls, sessions = stripe
    assert http.get('/api/shop/config').json() == {'checkout': 'stripe', 'stripe_mode': 'test', 'publishable_key': 'pk_test_' + 'y' * 24}
    listing = register(http, '573741401425').json()['listing']
    assert http.post('/api/shop/checkout', json={'product_id': 'f' * 32}).status_code == 404
    assert http.post('/api/shop/checkout', json={'product_id': listing['id'], 'price_jpy': 1}).status_code == 201
    response = http.post('/api/shop/checkout', json={'product_id': listing['id'], 'quantity': 2})
    assert response.status_code == 201 and response.json()['url'].startswith('https://checkout.stripe.com/')
    data = calls[-1][2]
    assert data['line_items[0][price_data][unit_amount]'] == listing['price_jpy'] and data['line_items[0][quantity]'] == 2
    assert data['line_items[0][price_data][currency]'] == 'jpy' and data['phone_number_collection[enabled]'] == 'true'
    assert data['shipping_address_collection[allowed_countries][0]'] == 'JP'
    assert data['success_url'] == 'https://shop.example/shop?checkout=success&session_id={CHECKOUT_SESSION_ID}'
    assert data['cancel_url'] == f"https://shop.example/products/{listing['id']}?checkout=cancel"
    unpaid = http.post('/api/shop/checkout/confirm', json={'session_id': SESSION})
    assert unpaid.status_code == 409 and http.get('/api/shop/admin/orders', headers=AUTH).json()['orders'] == []
    pay(sessions[SESSION])
    first = http.post('/api/shop/checkout/confirm', json={'session_id': SESSION}).json()
    again = http.post('/api/shop/checkout/confirm', json={'session_id': SESSION}).json()
    assert first == again and first['status'] == 'paid' and first['total_jpy'] == listing['price_jpy'] * 2
    assert 'phone' not in first and 'address' not in first
    [order] = http.get('/api/shop/admin/orders', headers=AUTH).json()['orders']
    assert order['name'] == '山田 太郎' and order['phone'] == '+819012345678' and order['email'] == 'taro@example.com'
    assert order['address'] == {'postal_code': '150-0001', 'state': '東京都', 'city': '渋谷区', 'line1': '神宮前1-2-3', 'line2': '101', 'country': 'JP'}
    assert order['title'] == listing['title'] and order['quantity'] == 2 and order['stripe_mode'] == 'test' and order['stripe_session_id'] == SESSION
    assert http.post('/api/shop/checkout/confirm', json={'session_id': 'cs_test_bad!'}).status_code == 422


def test_variant_must_be_listed(stripe):
    http, _, _ = stripe
    listing = register(http, '573741401425').json()['listing']
    response = http.post('/api/shop/checkout', json={'product_id': listing['id'], 'variant': '偽物'})
    assert response.status_code == 422 and response.json()['code'] == 'variant_unavailable'


def sign(payload, secret, t=None):
    t = int(t or time.time())
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.'.encode() + payload, hashlib.sha256).hexdigest()}"


def test_webhook_verifies_signature_and_is_idempotent_with_confirm(stripe, monkeypatch):
    http, _, sessions = stripe
    assert http.post('/api/shop/stripe/webhook', content=b'{}').status_code == 503
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', 'whsec_' + 'z' * 20)
    listing = register(http, '573741401425').json()['listing']
    http.post('/api/shop/checkout', json={'product_id': listing['id']})
    pay(sessions[SESSION])
    payload = json.dumps({'type': 'checkout.session.completed', 'data': {'object': sessions[SESSION]}}).encode()
    bad = http.post('/api/shop/stripe/webhook', content=payload, headers={'stripe-signature': sign(payload, 'whsec_wrong')})
    old = http.post('/api/shop/stripe/webhook', content=payload, headers={'stripe-signature': sign(payload, 'whsec_' + 'z' * 20, time.time() - 600)})
    assert bad.status_code == 400 and old.status_code == 400
    ok = http.post('/api/shop/stripe/webhook', content=payload, headers={'stripe-signature': sign(payload, 'whsec_' + 'z' * 20)})
    assert ok.status_code == 200
    http.post('/api/shop/checkout/confirm', json={'session_id': SESSION})
    assert len(http.get('/api/shop/admin/orders', headers=AUTH).json()['orders']) == 1


def test_badges_need_real_list_price_and_paid_sales(stripe, monkeypatch):
    http, _, sessions = stripe
    monkeypatch.setenv('KARRY_SHOP_BESTSELLER_MIN', '2')
    listing = register(http, '573741401425').json()['listing']
    item = http.get('/api/shop/products').json()['products'][0]
    assert item['discount_percent'] is None and item['list_price_jpy'] is None and item['bestseller'] is False and item['free_shipping'] is True
    url = f"/api/shop/admin/products/{listing['id']}"
    assert http.patch(url, json={'list_price_jpy': 9999}).status_code == 401
    assert http.patch(url, json={'list_price_jpy': listing['price_jpy']}, headers=AUTH).status_code == 422
    list_price = listing['price_jpy'] * 2 + 1
    assert http.patch(url, json={'list_price_jpy': list_price}, headers=AUTH).json()['product']['discount_percent'] == 50
    http.post('/api/shop/orders', json={'product_id': listing['id'], 'quantity': 5, 'name': '申込', 'contact': 'a@example.com'})
    assert http.get(f"/api/shop/products/{listing['id']}").json()['product']['bestseller'] is False
    http.post('/api/shop/checkout', json={'product_id': listing['id'], 'quantity': 2})
    pay(sessions[SESSION])
    http.post('/api/shop/checkout/confirm', json={'session_id': SESSION})
    item = http.get('/api/shop/products').json()['products'][0]
    assert item['bestseller'] is True and item['list_price_jpy'] == list_price
    register(http, '573741401425')
    assert http.get('/api/shop/products').json()['products'][0]['list_price_jpy'] == list_price
    assert http.patch(url, json={'list_price_jpy': None}, headers=AUTH).json()['product']['discount_percent'] is None


def test_verify_webhook_rejects_garbage():
    assert stripe_checkout.verify_webhook(b'{}', 'nonsense', 'whsec_x') is False
    assert stripe_checkout.verify_webhook(b'{}', 't=abc,v1=00', 'whsec_x') is False
    assert shop_store.public_product({'price_jpy': 1000, 'list_price_jpy': 999})['discount_percent'] is None
