import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as web
from karry_agent import alibaba1688 as ali
from karry_agent import (
    alibaba_api,
    alibaba_scrape,
    product_images,
    shop_api,
    shop_store,
)

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


@pytest.mark.parametrize('value,expected', [('573741401425', 'https://detail.1688.com/offer/573741401425.html'),
                                            ('detail.1688.com/offer/1.html?spm=a', 'https://detail.1688.com/offer/1.html?spm=a'),
                                            ('https://m.1688.com/x?offerId=6543210987', 'https://m.1688.com/x?offerId=6543210987')])
def test_only_1688_urls_are_accepted(value, expected):
    assert alibaba_scrape.normalize_url(value) == expected


@pytest.mark.parametrize('value', ['https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688',
                                   'rakumart.com/ProductDetails?fromPlatform=1688&goods_id=573741401425&mid=x',
                                   'https://www.rakumart.cn/ProductDetails?goods_id=573741401425'])
def test_rakumart_urls_open_the_1688_offer(value):
    assert alibaba_scrape.normalize_url(value) == 'https://detail.1688.com/offer/573741401425.html'
    assert alibaba_scrape.rakumart_offer_id(value) == '573741401425'


@pytest.mark.parametrize('value,code', [('https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=taobao', 'unsupported_platform'),
                                        ('https://www.rakumart.com/ProductDetails?goods_id=abc', 'invalid_product'),
                                        ('https://www.rakumart.com/', 'invalid_product'),
                                        ('https://rakumart.com.evil.example/ProductDetails?goods_id=573741401425', 'invalid_product')])
def test_bad_rakumart_urls_are_rejected(value, code):
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.normalize_url(value)
    assert exc.value.code == code


def test_rakumart_intake_reads_the_1688_page_and_notes_the_source():
    product = alibaba_scrape.intake('https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688', cost_cny=8.5,
                                    session=Session(Response(302, location='https://login.1688.com/')))
    assert product['product_id'] == '573741401425' and 'ラクマート' in product['notes'][0]


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


def test_manual_cost_title_and_category_win_and_a_blocked_page_is_only_a_note():
    product = alibaba_scrape.intake('573741401425', cost_cny=15, session=Session(Response(200, page())))
    assert product['min_price_cny'] == 15 and product['title'].startswith('北欧风') and not product['title_missing']
    blocked = Session(Response(302, location='https://login.1688.com/'))
    product = alibaba_scrape.intake('https://detail.1688.com/offer/573741401425.html', cost_cny=8.5, title='硅胶折叠水杯',
                                    category='杯子', session=blocked)
    assert (product['product_id'], product['title'], product['category'], product['min_price_cny']) == ('573741401425', '硅胶折叠水杯', '杯子', 8.5)
    assert product['url'] == 'https://detail.1688.com/offer/573741401425.html' and '入力した値' in ' '.join(product['notes'])
    product = alibaba_scrape.intake('573741401425', cost_cny=8.5, session=Session(Response(302, location='https://login.1688.com/')))
    assert product['title_missing'] and product['title'] == '1688 商品 573741401425'


def test_without_cost_a_blocked_page_or_bad_url_is_an_error():
    with pytest.raises(ali.AlibabaError) as exc:
        alibaba_scrape.intake('573741401425', session=Session(Response(302, location='https://login.1688.com/')))
    assert exc.value.code == 'page_blocked' and '原価' in exc.value.message
    for bad in ('', 'https://evil.example/offer/1.html'):
        with pytest.raises(ali.AlibabaError) as exc:
            alibaba_scrape.intake(bad, cost_cny=10)
        assert exc.value.code == 'invalid_product'
    assert not hasattr(alibaba_scrape, 'decode_screenshot') and not hasattr(alibaba_scrape, 'gemini_extract')


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
    monkeypatch.setattr(product_images, 'download', lambda url, session=None: None)
    monkeypatch.setattr(product_images, 'gemini_enhance', lambda photo, title: None)
    state = {'html': page(), 'blocked': False}

    def intake(url, cost, title, category):
        response = Response(302, location='https://login.1688.com/') if state['blocked'] else Response(200, state['html'])
        return alibaba_scrape.intake(url, cost, title, category, session=Session(response))

    test_app = FastAPI()
    router = alibaba_api.build_router(web.require_tool_auth, intake)
    test_app.include_router(router)
    public, owner = shop_api.build_routers(web.require_tool_auth)
    test_app.include_router(public)
    test_app.include_router(owner)
    return TestClient(test_app), state, router


def test_evaluate_returns_review_and_price_and_saves_a_private_draft(api):
    http, _, _ = api
    body = http.post('/api/alibaba/evaluate', json={'product': 'https://detail.1688.com/offer/573741401425.html',
                                                    'pricing': {'cny_jpy_rate': 20}}, headers=AUTH)
    assert body.status_code == 200 and body.headers['cache-control'] == 'no-store'
    data = body.json()
    assert data['product']['title'].startswith('北欧风') and data['review']['verdict'] == 'SAFE'
    assert data['pricing']['cost_cny'] == 9.8 and data['pricing']['config']['cny_jpy_rate'] == 20 and data['pricing']['selling_price_jpy'] > 0
    drafts = http.get('/api/alibaba/drafts', headers=AUTH).json()['drafts']
    assert [d['id'] for d in drafts] == [data['draft']['id']] and drafts[0]['pricing']['selling_price_jpy'] == data['pricing']['selling_price_jpy']
    assert http.get('/api/shop/products').json()['products'] == []
    again = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'cost_cny': 100}, headers=AUTH).json()
    assert again['draft']['id'] == data['draft']['id'] and again['pricing']['selling_price_jpy'] == 4914
    assert http.post('/api/alibaba/fetch', json={'product': '573741401425'}, headers=AUTH).json()['product']['images']
    bad = http.post('/api/alibaba/evaluate', json={'product': 'https://evil.example/offer/1.html'}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_product'
    bad = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'pricing': {'markup': 9}}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_pricing'
    assert http.post('/api/alibaba/evaluate', json={'product': '1', 'cost_cny': 0}, headers=AUTH).status_code == 422
    assert http.delete(f"/api/alibaba/drafts/{data['draft']['id']}", headers=AUTH).json()['removed'] is True
    assert http.get('/api/alibaba/drafts', headers=AUTH).json()['drafts'] == []


def test_evaluate_without_price_still_returns_review(api):
    http, state, _ = api
    state['html'] = page(prices=())
    data = http.post('/api/alibaba/evaluate', json={'product': '573741401425'}, headers=AUTH).json()
    assert data['pricing'] is None and '原価' in data['pricing_error'] and data['review']['verdict'] == 'SAFE'
    response = http.post('/api/alibaba/publish', json={'draft_id': data['draft']['id']}, headers=AUTH)
    assert response.status_code == 409 and '原価' in response.json()['reason']


def test_blocked_page_with_manual_cost_makes_a_draft_without_error_then_registers_it(api):
    http, state, _ = api
    state['blocked'] = True
    assert http.post('/api/alibaba/evaluate', json={'product': '573741401425'}, headers=AUTH).json()['code'] == 'page_blocked'
    untitled = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'cost_cny': 100}, headers=AUTH).json()
    assert untitled['pricing']['selling_price_jpy'] == 4914 and untitled['product']['title_missing']
    response = http.post('/api/alibaba/publish', json={'draft_id': untitled['draft']['id']}, headers=AUTH)
    assert response.status_code == 409 and '商品名' in response.json()['reason']
    data = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'cost_cny': 100, 'title': '硅胶折叠水杯',
                                                    'category': '杯子'}, headers=AUTH)
    assert data.status_code == 200 and data.json()['draft']['id'] == untitled['draft']['id']
    response = http.post('/api/alibaba/publish', json={'draft_id': data.json()['draft']['id']}, headers=AUTH)
    assert response.status_code == 201 and response.json()['listing']['price_jpy'] == 4914
    assert http.get('/api/shop/products').json()['products'][0]['title'] == '硅胶折叠水杯'
    assert http.get('/api/alibaba/drafts', headers=AUTH).json()['drafts'] == []
    missing = http.post('/api/alibaba/publish', json={'draft_id': 'f' * 32}, headers=AUTH)
    assert missing.status_code == 404 and missing.json()['code'] == 'draft_not_found'
    assert http.get('/api/shop/images/' + 'f' * 32 + '.jpg').status_code == 404


def test_api_requires_owner_token_and_needs_no_1688_keys(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    monkeypatch.setenv('GEMINI_API_KEY', GEMINI_SECRET)
    for name in ('KARRY_1688_APP_KEY', 'KARRY_1688_APP_SECRET', 'KARRY_1688_ACCESS_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    http = TestClient(web.app)
    for method, path in (('get', '/api/alibaba/config'), ('get', '/api/alibaba/drafts'), ('post', '/api/alibaba/evaluate'),
                         ('post', '/api/alibaba/publish'), ('delete', '/api/alibaba/drafts/' + 'f' * 32),
                         ('get', '/api/shop/admin/products'), ('get', '/api/admin/me')):
        assert getattr(http, method)(path).status_code == 401, path
        assert getattr(http, method)(path, headers={'Authorization': 'Bearer wrong'}).status_code == 401, path
    assert http.get('/api/admin/me', headers=AUTH).json() == {'admin': True}
    response = http.get('/api/alibaba/config', headers=AUTH)
    config = response.json()
    assert response.status_code == 200 and config['pricing']['cny_jpy_rate'] == 22
    assert GEMINI_SECRET not in response.text and 'KARRY_1688_APP_KEY' not in response.text and 'vision' not in config


def test_admin_endpoints_fail_closed_without_a_configured_token(monkeypatch):
    monkeypatch.delenv('KARRY_API_TOKEN', raising=False)
    http = TestClient(web.app)
    assert http.get('/api/admin/me').status_code == 503 and http.get('/api/alibaba/drafts').status_code == 503


def test_admin_page_is_noindex_and_shop_stays_public(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    http = TestClient(web.app)
    page_ = http.get('/admin')
    assert page_.status_code == 200 and page_.headers['x-robots-tag'] == 'noindex' and '法務・AIダッシュボード' in page_.text
    assert OWNER not in page_.text and http.get('/shop').status_code == 200


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


def test_evaluate_with_photos_stores_enhanced_shop_images_and_publishes_them(api, monkeypatch):
    import io

    from PIL import Image
    http, state, _ = api
    out = io.BytesIO()
    Image.new('RGB', (300, 300), (20, 90, 160)).save(out, 'PNG')
    photo = 'data:image/png;base64,' + __import__('base64').b64encode(out.getvalue()).decode()
    seen = []
    monkeypatch.setattr(product_images, 'gemini_enhance', lambda body, title: seen.append(title) or body)
    state['blocked'] = True
    data = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'cost_cny': 100, 'title': '硅胶折叠水杯',
                                                    'photos': [photo]}, headers=AUTH).json()
    assert data['product']['image_processing'] == {'engine': 'gemini', 'sources': 1, 'enhanced': 1} and seen == ['硅胶折叠水杯']
    image = data['product']['images'][0]
    served = http.get(image)
    assert served.status_code == 200 and served.headers['content-type'] == 'image/jpeg' and served.content[:2] == b'\xff\xd8'
    listing = http.post('/api/alibaba/publish', json={'draft_id': data['draft']['id']}, headers=AUTH).json()['listing']
    assert listing['images'] == [image] and http.get('/api/shop/products').json()['products'][0]['images'] == [image]
    bad = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'cost_cny': 1, 'photos': ['data:image/png;base64,***']},
                    headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_photo'
    assert http.post('/api/alibaba/evaluate', json={'product': '1', 'photos': ['x'] * 5}, headers=AUTH).status_code == 422
    assert http.get('/api/shop/images/..%2Fcatalog.jpg').status_code == 404


def test_bookmarklet_image_urls_are_used_only_from_alicdn_when_the_page_is_blocked(api, monkeypatch):
    http, state, _ = api
    seen = []
    monkeypatch.setattr(product_images, 'download', lambda url, session=None: seen.append(url))
    state['blocked'] = True
    data = http.post('/api/alibaba/evaluate', json={
        'product': 'https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688', 'cost_cny': 12.8,
        'title': 'シリコン折りたたみカップ', 'enhance_images': False,
        'image_urls': ['//cbu01.alicdn.com/img/a.jpg', 'https://evil.example/x.jpg', 'http://127.0.0.1/y.jpg']}, headers=AUTH).json()
    assert seen == [] and data['product']['images'] == ['https://cbu01.alicdn.com/img/a.jpg']
    assert data['product']['url'] == 'https://detail.1688.com/offer/573741401425.html' and data['pricing']['cost_cny'] == 12.8
    too_many = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'image_urls': ['https://cbu01.alicdn.com/a.jpg'] * 5},
                         headers=AUTH)
    assert too_many.status_code == 422
