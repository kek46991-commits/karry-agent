import base64
import io
import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import app as web
from karry_agent import alibaba1688 as ali
from karry_agent import alibaba_api, alibaba_scrape, shop_api, shop_store

OWNER = 'synthetic-owner-' + 'token'
AUTH = {'Authorization': 'Bearer ' + OWNER}
GEMINI_SECRET = 'synthetic-gemini-' + 'key'


def page(title='北欧风陶瓷马克杯 咖啡杯', prices=('12.50', '9.80'), images=3, extra=''):
    imgs = ''.join(f'"//cbu01.alicdn.com/img/ibank/O1CN{i}.jpg_400x400.jpg",' for i in range(images))
    price = ','.join(f'"price":"{p}"' for p in prices)
    return (f'<html><head><title>{title} - 阿里巴巴</title><meta name="description" content="简约 陶瓷">'
            f'<meta property="og:image" content="https://cbu01.alicdn.com/img/ibank/O1CN0.jpg"></head><body>'
            f'<script>var d={{{price},"categoryName":"杯子",imgs:[{imgs}"https://evil.example/x.jpg"]}};</script>{extra}</body></html>')


class Response:
    def __init__(self, status=200, body='', location=''):
        self.status_code, self.body, self.headers, self.encoding = status, body.encode(), {'Location': location}, 'utf-8'
        self.closed = False

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers, timeout, allow_redirects, stream):
        self.calls.append({'url': url, 'headers': headers, 'allow_redirects': allow_redirects})
        return self.responses.pop(0)


def shot(color=(200, 30, 30), size=(400, 300), fmt='PNG'):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, fmt)
    return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode()


@pytest.mark.parametrize('value,expected', [('573741401425', 'https://detail.1688.com/offer/573741401425.html'),
                                            ('detail.1688.com/offer/1.html?spm=a', 'https://detail.1688.com/offer/1.html?spm=a'),
                                            ('https://m.1688.com/x?offerId=6543210987', 'https://m.1688.com/x?offerId=6543210987')])
def test_only_1688_urls_are_accepted(value, expected):
    assert alibaba_scrape.normalize_url(value) == expected


@pytest.mark.parametrize('value', ['https://evil.example/offer/1.html', 'https://1688.com.evil.example/offer/1.html',
                                   'ftp://detail.1688.com/x', 'https://detail.1688.com:8443/x', 'http://127.0.0.1/', 'file:///etc/passwd'])
def test_other_hosts_are_rejected(value):
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.normalize_url(value)
    assert exc.value.code == 'invalid_product'


@pytest.mark.parametrize('value,expected', [('573741401425', '573741401425'),
                                            ('https://detail.1688.com/offer/573741401425.html?spm=a', '573741401425'),
                                            ('https://m.1688.com/x?offerId=6543210987', '6543210987')])
def test_product_id_parsing(value, expected):
    assert ali.parse_product_id(value) == expected


@pytest.mark.parametrize('value', ['', 'hello', 'https://example.com/offer/abc.html', '12345'])
def test_product_id_rejects_junk(value):
    with pytest.raises(ali.AlibabaError) as exc:
        ali.parse_product_id(value)
    assert exc.value.code == 'invalid_product'


def test_page_is_fetched_once_without_cookies_and_parsed():
    session = Session(Response(200, page()))
    final, html = alibaba_scrape.fetch_page('573741401425', session=session)
    assert len(session.calls) == 1 and session.calls[0]['allow_redirects'] is False and 'Cookie' not in session.calls[0]['headers']
    product = alibaba_scrape.parse_page(html, final)
    assert product['product_id'] == '573741401425' and product['title'] == '北欧风陶瓷马克杯 咖啡杯' and product['category'] == '杯子'
    assert product['images'] == [f'https://cbu01.alicdn.com/img/ibank/O1CN{i}.jpg' for i in range(3)]
    assert product['min_price_cny'] == 9.8 and product['max_price_cny'] == 12.5 and product['description_excerpt'] == '简约 陶瓷'


@pytest.mark.parametrize('responses,code', [
    ([Response(302, location='https://login.1688.com/member/signin.htm')], 'page_blocked'),
    ([Response(200, page(extra='<div id="nocaptcha">请拖动下方滑块完成验证</div>'))], 'page_blocked'),
    ([Response(302, location='/punish?x5secdata=abc')], 'page_blocked'),
    ([Response(403)], 'page_blocked'),
    ([Response(429)], 'page_blocked'),
    ([Response(503)], 'page_unavailable'),
    ([Response(404)], 'product_not_found'),
    ([Response(302, location='https://evil.example/steal')], 'invalid_product'),
])
def test_blocked_or_foreign_pages_are_not_bypassed(responses, code):
    session = Session(*responses)
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.fetch_page('https://detail.1688.com/offer/573741401425.html', session=session)
    assert exc.value.code == code and len(session.calls) == 1


def test_redirects_inside_1688_are_followed():
    session = Session(Response(301, location='https://detail.1688.com/offer/573741401425.html'), Response(200, page()))
    assert alibaba_scrape.fetch_page('https://m.1688.com/offer/573741401425.html', session=session)[0].startswith('https://detail.1688.com/')


def test_screenshot_is_read_by_gemini_vision_and_product_photo_cropped(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    calls = []

    def extractor(images=None, text=''):
        calls.append(images)
        return {'title': '硅胶折叠水杯', 'category': '杯子', 'price_cny': '8.5', 'specs': [{'name': '材质', 'value': '硅胶'}],
                'variants': ['蓝色'], 'description': '可折叠', 'product_box': [0, 0, 500, 500]}

    product = alibaba_scrape.intake('', [shot(size=(800, 600))], extractor=extractor)
    assert calls and calls[0][0][:2] == b'\xff\xd8' and product['source'] == 'screenshot'
    assert product['title'] == '硅胶折叠水杯' and product['min_price_cny'] == 8.5 and product['attributes'] == [{'name': '材质', 'value': '硅胶'}]
    assert product['product_id'].startswith('shot-') and product['images'] == []
    with Image.open(io.BytesIO(product['screenshot_images'][0])) as cropped:
        assert cropped.size == (400, 300)


def test_shop_images_fit_the_storage_document_limit():
    noisy = Image.effect_noise((1600, 1600), 120).convert('RGB')
    data = alibaba_scrape.shop_jpeg(noisy, limit=200_000)
    assert len(data) <= 200_000 and data[:2] == b'\xff\xd8'


def test_durable_store_round_trips_screenshot_images(monkeypatch):
    docs = {}

    class Repo:
        prefix = 'p/'

        def _read(self, key):
            return docs.get(key), None

        def _cas(self, key, default, mutate):
            docs[key] = mutate(docs.get(key, default))
            return docs[key]

    monkeypatch.setattr(shop_store.task_persistence, 'mode', lambda: 's3')
    monkeypatch.setattr(shop_store.task_persistence, 'repository', lambda: Repo())
    store = shop_store.ShopStore()
    url = store.save_image(b'\xff\xd8jpeg')
    assert store.image(url.split('/')[-1][:-4]) == b'\xff\xd8jpeg' and list(docs) == [f"p/shop/images/{url.split('/')[-1][:-4]}.json"]


def test_blocked_url_falls_back_to_the_screenshot():
    session = Session(Response(302, location='https://login.1688.com/'))
    product = alibaba_scrape.intake('573741401425', [shot()], session=session,
                                    extractor=lambda images=None, text='': {'title': '陶瓷杯', 'price_cny': 10})
    assert product['product_id'] == '573741401425' and product['min_price_cny'] == 10 and 'スクショから読み取りました' in ' '.join(product['notes'])
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.intake('573741401425', [], session=Session(Response(302, location='https://login.1688.com/')))
    assert exc.value.code == 'page_blocked' and 'スクショ' in exc.value.message


def test_screenshot_errors(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    for value, code in [('data:image/png;base64,***', 'invalid_screenshot'),
                        ('data:image/png;base64,' + base64.b64encode(b'not an image').decode(), 'invalid_screenshot'),
                        (shot(), 'vision_unavailable')]:
        with pytest.raises(ali.AlibabaError) as exc:
            alibaba_scrape.intake('', [value])
        assert exc.value.code == code

    def broken(images=None, text=''):
        raise RuntimeError(GEMINI_SECRET)
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.intake('', [shot()], extractor=broken)
    assert exc.value.code == 'vision_failed' and GEMINI_SECRET not in exc.value.message
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.intake('', [shot()], extractor=lambda images=None, text='': {})
    assert exc.value.code == 'product_unreadable'
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.intake('', [])
    assert exc.value.code == 'invalid_product'


def test_manual_cost_overrides_and_page_text_goes_to_gemini_when_price_missing():
    seen = []
    product = alibaba_scrape.intake('573741401425', session=Session(Response(200, page(prices=()))),
                                    extractor=lambda images=None, text='': seen.append(text) or {'price_cny': 7})
    assert seen and '北欧风' in seen[0] and product['min_price_cny'] == 7 and product['source'] == 'page'
    product = alibaba_scrape.intake('573741401425', cost_cny=15, session=Session(Response(200, page())))
    assert product['min_price_cny'] == 15


def product(title, **extra):
    return {'title': title, 'category': '', 'attributes': [], 'description_excerpt': '', **extra}


def test_rule_screen_flags_brand_cosmetic_and_mains_items(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    assert ali.review_product(product('北欧风陶瓷马克杯'))['verdict'] == 'SAFE'
    for title, check in (('耐克同款运动鞋', 'trademark'), ('美白面霜 化妆品', 'pharmaceutical'), ('22.5W 充电宝 移动电源', 'pse')):
        result = ali.review_product(product(title))
        assert result['verdict'] == 'REJECT' and result['checks'][check]['status'] == 'REJECT' and result['engine'] == 'rules'
    assert 'lv' not in ali.rule_screen(product('Solve problems'))['trademark']['reason']


def test_gemini_and_rules_are_combined_fail_safe():
    safe = {c: {'status': 'SAFE', 'reason': 'ok'} for c in ali.CHECKS}
    assert ali.review_product(product('陶瓷杯'), generator=lambda p: safe)['verdict'] == 'SAFE'
    result = ali.review_product(product('充电器'), generator=lambda p: safe)
    assert result['verdict'] == 'REJECT' and result['checks']['pse']['status'] == 'REJECT' and result['engine'] == 'gemini+rules'
    risky = {**safe, 'trademark': {'status': 'REJECT', 'reason': 'キャラクター商品の疑い'}}
    result = ali.review_product(product('陶瓷杯'), generator=lambda p: risky)
    assert result['verdict'] == 'REJECT' and result['checks']['trademark']['reason'] == 'キャラクター商品の疑い'
    weird = ali.review_product(product('陶瓷杯'), generator=lambda p: {'trademark': {'status': 'maybe'}})
    assert weird['verdict'] == 'REJECT'
    def boom(_):
        raise RuntimeError('model down')
    assert ali.review_product(product('陶瓷杯'), generator=boom)['engine'] == 'rules'


def test_pricing_formula_and_validation(monkeypatch):
    for name in ali._PRICING_ENV.values():
        monkeypatch.delenv(name, raising=False)
    assert ali.pricing_defaults() == {'cny_jpy_rate': 22.0, 'agent_fee_rate': 0.05, 'shipping_jpy': 1200.0, 'markup': 1.4}
    result = ali.price_product(100)
    # 100 RMB × 22 = 2,200 + 5 % (110) + 1,200 = 3,510 → × 1.4 = 4,914
    assert (result['goods_jpy'], result['agent_fee_jpy'], result['shipping_jpy'], result['landed_cost_jpy']) == (2200, 110, 1200, 3510)
    assert result['selling_price_jpy'] == 4914 and result['profit_jpy'] == 1404 and result['margin_rate'] == pytest.approx(0.2857, abs=1e-4)
    assert ali.price_product(9.8)['selling_price_jpy'] == math.ceil((9.8 * 22 * 1.05 + 1200) * 1.4)
    assert ali.price_product(10, {'cny_jpy_rate': 20, 'agent_fee_rate': 0, 'shipping_jpy': 100, 'markup': 2})['selling_price_jpy'] == 600
    monkeypatch.setenv('KARRY_CNY_JPY_RATE', '21')
    monkeypatch.setenv('KARRY_SHOP_MARKUP', '9')
    assert ali.pricing_defaults()['cny_jpy_rate'] == 21 and ali.pricing_defaults()['markup'] == 1.4
    for bad in ({'markup': 0.9}, {'cny_jpy_rate': -1}, {'shipping_jpy': 'x'}):
        with pytest.raises(ali.AlibabaError) as exc:
            ali.price_product(10, bad)
        assert exc.value.code == 'invalid_pricing'
    with pytest.raises(ali.AlibabaError):
        ali.price_product(None)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setattr(shop_store.task_persistence, 'mode', lambda: 'memory')
    shop_store._MEMORY.clear()
    shop_store._IMAGES.clear()
    state = {'html': page(), 'blocked': False, 'extract': {'title': '硅胶折叠水杯', 'category': '杯子', 'price_cny': 8.5}}

    def intake(url, screenshots, cost):
        response = Response(302, location='https://login.1688.com/') if state['blocked'] else Response(200, state['html'])
        return alibaba_scrape.intake(url, screenshots, cost, session=Session(response),
                                     extractor=lambda images=None, text='': state['extract'])

    test_app = FastAPI()
    router = alibaba_api.build_router(web.require_tool_auth, intake)
    test_app.include_router(router)
    public, owner = shop_api.build_routers(web.require_tool_auth)
    test_app.include_router(public)
    test_app.include_router(owner)
    return TestClient(test_app), state, router


def test_evaluate_endpoint_returns_product_review_and_price(api):
    http, _, _ = api
    body = http.post('/api/alibaba/evaluate', json={'product': 'https://detail.1688.com/offer/573741401425.html',
                                                    'pricing': {'cny_jpy_rate': 20}}, headers=AUTH)
    assert body.status_code == 200 and body.headers['cache-control'] == 'no-store'
    data = body.json()
    assert data['product']['title'].startswith('北欧风') and data['review']['verdict'] == 'SAFE'
    assert data['pricing']['cost_cny'] == 9.8 and data['pricing']['config']['cny_jpy_rate'] == 20 and data['pricing']['selling_price_jpy'] > 0
    assert http.post('/api/alibaba/fetch', json={'product': '573741401425'}, headers=AUTH).json()['product']['images']
    bad = http.post('/api/alibaba/evaluate', json={'product': 'https://evil.example/offer/1.html'}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_product'
    bad = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'pricing': {'markup': 9}}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_pricing'
    assert http.post('/api/alibaba/evaluate', json={'product': '1', 'screenshots': ['x'] * 5}, headers=AUTH).status_code == 422


def test_evaluate_without_price_still_returns_review(api):
    http, state, _ = api
    state['html'], state['extract'] = page(prices=()), {}
    data = http.post('/api/alibaba/evaluate', json={'product': '573741401425'}, headers=AUTH).json()
    assert data['pricing'] is None and '原価' in data['pricing_error'] and data['review']['verdict'] == 'SAFE'


def test_blocked_page_asks_for_a_screenshot_then_publishes_from_it(api):
    http, state, _ = api
    state['blocked'] = True
    blocked = http.post('/api/alibaba/publish', json={'product': '573741401425'}, headers=AUTH)
    assert blocked.status_code == 422 and blocked.json()['code'] == 'page_blocked' and 'スクショ' in blocked.json()['detail']
    response = http.post('/api/alibaba/publish', json={'product': '573741401425', 'screenshots': [shot()]}, headers=AUTH)
    assert response.status_code == 201 and response.json()['published'] is True
    data = response.json()
    assert 'screenshot_images' not in data['product'] and data['pricing']['selling_price_jpy'] == ali.price_product(8.5)['selling_price_jpy']
    image = data['listing']['images'][0]
    assert image.startswith('/api/shop/images/') and image.endswith('.jpg')
    served = http.get(image)
    assert served.status_code == 200 and served.headers['content-type'] == 'image/jpeg' and served.content[:2] == b'\xff\xd8'
    assert http.get('/api/shop/images/' + 'f' * 32 + '.jpg').status_code == 404
    assert http.get('/api/shop/images/..%2Fcatalog.jpg').status_code == 404
    assert http.get('/api/shop/products').json()['products'][0]['title'] == '硅胶折叠水杯'


def test_api_requires_owner_token_and_needs_no_1688_keys(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    monkeypatch.setenv('GEMINI_API_KEY', GEMINI_SECRET)
    for name in ('KARRY_1688_APP_KEY', 'KARRY_1688_APP_SECRET', 'KARRY_1688_ACCESS_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    http = TestClient(web.app)
    assert http.get('/api/alibaba/config').status_code == 401
    assert http.post('/api/alibaba/evaluate', json={'product': '573741401425'}).status_code == 401
    response = http.get('/api/alibaba/config', headers=AUTH)
    config = response.json()
    assert response.status_code == 200 and config['mode'] == 'page+vision' and config['vision'] is True and config['pricing']['cny_jpy_rate'] == 22
    assert GEMINI_SECRET not in response.text and 'KARRY_1688_APP_KEY' not in response.text and 'configured' not in config


def test_legal_dashboard_logs_errors_and_verdicts_without_secrets(api):
    http, state, router = api
    state['html'] = page(title='22.5W 充电宝')
    assert http.get('/api/alibaba/logs').status_code == 401
    http.post('/api/alibaba/evaluate', json={'product': '573741401425'}, headers=AUTH)
    state['blocked'] = True
    assert http.post('/api/alibaba/evaluate', json={'product': '573741401426'}, headers=AUTH).status_code == 422
    http.post('/api/alibaba/fetch', json={'product': 'https://evil.example/'}, headers=AUTH)
    entries = http.get('/api/alibaba/logs', headers=AUTH).json()['entries']
    assert [(e['action'], e['status'], e.get('code'), e.get('verdict')) for e in entries] == [
        ('fetch', 'error', 'invalid_product', None), ('evaluate', 'error', 'page_blocked', None), ('evaluate', 'ok', None, 'REJECT')]
    assert entries[2]['checks']['pse']['status'] == 'REJECT' and entries[2]['images'] == 3 and entries[1]['product_id'] == '573741401426'
    assert OWNER not in str(entries)
    for _ in range(alibaba_api.MAX_LOG_ENTRIES + 5):
        router.audit.add(action='fetch', status='ok')
    assert len(router.audit.list(1000)) == alibaba_api.MAX_LOG_ENTRIES
