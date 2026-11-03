import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as web
from karry_agent import alibaba_api, alibaba_scrape, shop_api, shop_store

OWNER = 'synthetic-owner-' + 'token'
AUTH = {'Authorization': 'Bearer ' + OWNER}


def page(title, images=14):
    imgs = ','.join(f'"//cbu01.alicdn.com/img/ibank/{i}.jpg"' for i in range(images))
    return (f'<html><head><title>{title}</title></head><body><script>var d={{"price":"9.80","skuPrice":"11.00",'
            f'"categoryName":"杯子","images":[{imgs}]}};</script></body></html>')


class Reply:
    status_code, headers, encoding = 200, {}, 'utf-8'

    def __init__(self, body):
        self.body = body.encode()

    def iter_content(self, size):
        yield self.body

    def close(self):
        pass


@pytest.fixture
def shop(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setattr(shop_store.task_persistence, 'mode', lambda: 'memory')
    shop_store._MEMORY.clear()
    state = {'title': '北欧风陶瓷马克杯'}

    class Session:
        def get(self, url, **_):
            return Reply(page(state['title']))

    def intake(url, cost, title, category):
        return alibaba_scrape.intake(url, cost, title, category, session=Session())

    app = FastAPI()
    app.include_router(alibaba_api.build_router(web.require_tool_auth, intake))
    public, owner = shop_api.build_routers(web.require_tool_auth)
    app.include_router(public)
    app.include_router(owner)
    return TestClient(app), state


def register(http, product, **body):
    draft = http.post('/api/alibaba/evaluate', json={'product': product, **body}, headers=AUTH).json()['draft']
    return http.post('/api/alibaba/publish', json={'draft_id': draft['id']}, headers=AUTH)


def test_safe_product_is_published_and_visible_publicly(shop):
    http, _ = shop
    assert http.post('/api/alibaba/publish', json={'draft_id': 'f' * 32}).status_code == 401
    response = register(http, 'https://detail.1688.com/offer/573741401425.html', pricing={'cny_jpy_rate': 20})
    assert response.status_code == 201 and response.json()['published'] is True
    listing = response.json()['listing']
    assert len(listing['images']) == 12 and listing['price_jpy'] > 0 and listing['category'] == '杯子'
    catalog = http.get('/api/shop/products').json()
    assert catalog['categories'] == ['杯子'] and catalog['products'][0]['id'] == listing['id']
    assert {'specs', 'variants', 'images'} <= set(catalog['products'][0]) and 'review' not in catalog['products'][0]
    again = register(http, '573741401425', pricing={'cny_jpy_rate': 25}).json()
    products = http.get('/api/shop/products').json()['products']
    assert again['listing']['id'] == listing['id'] and len(products) == 1 and products[0]['price_jpy'] > listing['price_jpy']
    log = http.get('/api/alibaba/logs', headers=AUTH).json()['entries'][0]
    assert log['action'] == 'publish' and log['status'] == 'ok' and log['shop_id'] == listing['id'] and log['verdict'] == 'SAFE'
    assert http.get('/api/shop/admin/products').status_code == 401
    admin = http.get('/api/shop/admin/products', headers=AUTH).json()['products']
    assert admin[0]['id'] == listing['id'] and admin[0]['review']['verdict'] == 'SAFE' and admin[0]['source_url'].endswith('573741401425.html')


def test_rejected_product_is_never_listed(shop):
    http, state = shop
    state['title'] = '22.5W 充电宝 移动电源'
    response = register(http, '573741401427')
    assert response.status_code == 409 and response.json()['published'] is False and response.json()['draft']['review']['verdict'] == 'REJECT'
    assert 'REJECT' in response.json()['reason']
    assert http.get('/api/shop/products').json()['products'] == []
    assert http.get('/api/alibaba/logs', headers=AUTH).json()['entries'][0]['status'] == 'rejected'


def test_orders_are_public_to_create_and_owner_only_to_read(shop):
    http, _ = shop
    listing = register(http, '573741401425').json()['listing']
    bad = http.post('/api/shop/orders', json={'product_id': 'f' * 32, 'name': 'A', 'contact': 'a@example.com'})
    assert bad.status_code == 404
    assert http.post('/api/shop/orders', json={'product_id': listing['id'], 'name': '', 'contact': 'x'}).status_code == 422
    created = http.post('/api/shop/orders', json={'product_id': listing['id'], 'quantity': 3, 'name': '山田', 'contact': 'yamada@example.com'})
    assert created.status_code == 201 and created.json()['total_jpy'] == listing['price_jpy'] * 3
    assert http.get('/api/shop/admin/orders').status_code == 401
    orders = http.get('/api/shop/admin/orders', headers=AUTH).json()['orders']
    assert orders[0]['name'] == '山田' and orders[0]['quantity'] == 3 and orders[0]['title'] == listing['title']
    assert http.delete(f"/api/shop/admin/products/{listing['id']}", headers=AUTH).json()['removed'] is True
    assert http.get('/api/shop/products').json()['products'] == []


def test_shop_page_is_served_publicly(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    response = TestClient(web.app).get('/shop')
    assert response.status_code == 200 and 'kerry-admin-20261026' in response.text and 'KERRY ショップ' in response.text
