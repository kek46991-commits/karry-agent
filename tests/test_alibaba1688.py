import hashlib
import hmac
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as web
from karry_agent import alibaba1688 as ali
from karry_agent import alibaba_api

SECRET = 'synthetic-1688-' + 'secret'
TOKEN_1688 = 'synthetic-1688-' + 'access'
OWNER = 'synthetic-owner-' + 'token'
AUTH = {'Authorization': 'Bearer ' + OWNER}

INFO = {'productID': 573741401425, 'subject': '北欧风陶瓷马克杯 咖啡杯', 'categoryName': '杯子', 'status': 'published',
        'image': {'images': ['img/ibank/2014/766/624/1.jpg', '//cbu01.alicdn.com/img/ibank/2.jpg', 'https://evil.example/x.jpg']},
        'saleInfo': {'priceRanges': [{'startQuantity': 2, 'price': 12.5}, {'startQuantity': 100, 'price': 9.8}], 'unit': '个',
                     'minOrderQuantity': 2},
        'skuInfos': [{'skuId': 1, 'price': 11.0, 'amountOnSale': 300,
                      'attributes': [{'attributeDisplayName': '颜色', 'attributeValue': '白色', 'skuImageUrl': 'img/ibank/3.jpg'}]}],
        'attributes': [{'attributeName': '材质', 'value': '陶瓷'}], 'supplierLoginId': 'supplier01', 'description': '<p>简约</p>'}


def reply(status, body):
    response = Mock(status_code=status)
    response.json.return_value = body
    return response


def test_signature_matches_the_official_sdk_algorithm():
    path = 'param2/1/com.alibaba.product/alibaba.cross.productInfo/1234567'
    params = {'productId': '573741401425', 'access_token': TOKEN_1688}
    expected = hmac.new(SECRET.encode(), (path + 'access_token' + TOKEN_1688 + 'productId573741401425').encode(),
                        hashlib.sha1).hexdigest().upper()
    assert ali.sign(path, params, SECRET) == expected and len(expected) == 40


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


def test_open_api_request_shape_and_normalised_product():
    session = Mock()
    session.post.return_value = reply(200, {'success': True, 'productInfo': INFO})
    product = ali.OpenApiClient('1234567', SECRET, TOKEN_1688, session=session).product('573741401425')
    url, data = session.post.call_args.args[0], session.post.call_args.kwargs['data']
    assert url == 'https://gw.open.1688.com/openapi/param2/1/com.alibaba.product/alibaba.cross.productInfo/1234567'
    assert data['productId'] == '573741401425' and data['access_token'] == TOKEN_1688
    assert data['_aop_signature'] == ali.sign('param2/1/com.alibaba.product/alibaba.cross.productInfo/1234567',
                                              {'productId': '573741401425', 'access_token': TOKEN_1688}, SECRET)
    assert SECRET not in str(session.post.call_args)
    assert product['images'] == ['https://cbu01.alicdn.com/img/ibank/2014/766/624/1.jpg', 'https://cbu01.alicdn.com/img/ibank/2.jpg',
                                 'https://cbu01.alicdn.com/img/ibank/3.jpg']
    assert product['min_price_cny'] == 9.8 and product['max_price_cny'] == 12.5 and product['url'].endswith('/573741401425.html')
    assert product['skus'][0]['attributes'] == [{'name': '颜色', 'value': '白色'}] and product['description_excerpt'] == '简约'


@pytest.mark.parametrize('status,body,code', [
    (401, {'error_code': 'gw.AuthenticationFailed', 'error_message': 'secret ' + SECRET}, 'alibaba_auth'),
    (200, {'success': False, 'message': '商品[57374140142]不存在'}, 'product_not_found'),
    (200, {'success': False, 'message': '无权限查询此商品'}, 'product_forbidden'),
    (500, {'error_code': 'isp.error'}, 'alibaba_response'),
])
def test_open_api_errors_are_classified_without_leaking_secrets(status, body, code):
    session = Mock()
    session.post.return_value = reply(status, body)
    with pytest.raises(ali.AlibabaError) as exc:
        ali.OpenApiClient('k', SECRET, TOKEN_1688, session=session).product('573741401425')
    assert exc.value.code == code and SECRET not in exc.value.message and TOKEN_1688 not in exc.value.message


def test_missing_configuration_is_reported(monkeypatch):
    for name in ('KARRY_1688_APP_KEY', 'KARRY_1688_APP_SECRET', 'KARRY_1688_ACCESS_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ali.AlibabaError) as exc:
        ali.client_from_env()
    assert exc.value.code == 'alibaba_configuration' and exc.value.status_code == 503


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
    for name in ('KARRY_CNY_JPY_RATE', 'KARRY_1688_AGENT_FEE_RATE', 'KARRY_1688_SHIPPING_JPY', 'KARRY_1688_DUTY_RATE',
                 'KARRY_PLATFORM_FEE_RATE', 'KARRY_TARGET_MARGIN_RATE'):
        monkeypatch.delenv(name, raising=False)
    result = ali.price_product(10, {'cny_jpy_rate': 20, 'agent_fee_rate': 0, 'shipping_jpy': 100, 'duty_rate': 0,
                                    'platform_fee_rate': 0.1, 'target_margin_rate': 0.4})
    assert result['landed_cost_jpy'] == 300 and result['selling_price_jpy'] == 600 and result['profit_jpy'] == 240
    assert result['margin_rate'] == 0.4
    monkeypatch.setenv('KARRY_CNY_JPY_RATE', '22')
    assert ali.pricing_defaults()['cny_jpy_rate'] == 22
    monkeypatch.setenv('KARRY_CNY_JPY_RATE', 'abc')
    assert ali.pricing_defaults()['cny_jpy_rate'] == 21
    for bad in ({'target_margin_rate': 0.9, 'platform_fee_rate': 0.1}, {'cny_jpy_rate': -1}):
        with pytest.raises(ali.AlibabaError) as exc:
            ali.price_product(10, bad)
        assert exc.value.code == 'invalid_pricing'
    with pytest.raises(ali.AlibabaError):
        ali.price_product(None)


class FakeClient:
    def __init__(self, info=INFO):
        self.info, self.calls = info, []

    def product(self, product_id):
        self.calls.append(product_id)
        return ali.normalize_product(self.info)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    client = FakeClient()
    test_app = FastAPI()
    test_app.include_router(alibaba_api.build_router(web.require_tool_auth, lambda: client))
    return TestClient(test_app), client


def test_evaluate_endpoint_returns_product_review_and_price(api):
    http, client = api
    body = http.post('/api/alibaba/evaluate', json={'product': 'https://detail.1688.com/offer/573741401425.html',
                                                    'pricing': {'cny_jpy_rate': 20}}, headers=AUTH)
    assert body.status_code == 200 and client.calls == ['573741401425']
    data = body.json()
    assert data['product']['title'].startswith('北欧风') and data['review']['verdict'] == 'SAFE'
    assert data['pricing']['cost_cny'] == 9.8 and data['pricing']['config']['cny_jpy_rate'] == 20 and data['pricing']['selling_price_jpy'] > 0
    assert body.headers['cache-control'] == 'no-store'
    assert http.post('/api/alibaba/fetch', json={'product': '573741401425'}, headers=AUTH).json()['product']['images']
    bad = http.post('/api/alibaba/evaluate', json={'product': 'nope'}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_product'
    bad = http.post('/api/alibaba/evaluate', json={'product': '573741401425', 'pricing': {'target_margin_rate': 2}}, headers=AUTH)
    assert bad.status_code == 422 and bad.json()['code'] == 'invalid_pricing'


def test_evaluate_without_price_still_returns_review(api):
    http, client = api
    client.info = {**INFO, 'saleInfo': {}, 'skuInfos': []}
    data = http.post('/api/alibaba/evaluate', json={'product': '573741401425'}, headers=AUTH).json()
    assert data['pricing'] is None and '原価' in data['pricing_error'] and data['review']['verdict'] == 'SAFE'


def test_api_requires_owner_token_and_reports_configuration(monkeypatch):
    monkeypatch.setenv('KARRY_API_TOKEN', OWNER)
    for name in ('KARRY_1688_APP_KEY', 'KARRY_1688_APP_SECRET', 'KARRY_1688_ACCESS_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    http = TestClient(web.app)
    assert http.get('/api/alibaba/config').status_code == 401
    assert http.post('/api/alibaba/evaluate', json={'product': '573741401425'}).status_code == 401
    config = http.get('/api/alibaba/config', headers=AUTH).json()
    assert config['configured'] is False and 'KARRY_1688_APP_KEY' in config['reason'] and config['pricing']['cny_jpy_rate'] > 0
    response = http.post('/api/alibaba/evaluate', json={'product': '573741401425'}, headers=AUTH)
    assert response.status_code == 503 and response.json()['code'] == 'alibaba_configuration'
    monkeypatch.setenv('KARRY_1688_APP_KEY', 'k')
    monkeypatch.setenv('KARRY_1688_APP_SECRET', SECRET)
    monkeypatch.setenv('KARRY_1688_ACCESS_TOKEN', TOKEN_1688)
    text = http.get('/api/alibaba/config', headers=AUTH).text
    assert SECRET not in text and TOKEN_1688 not in text and '"configured":true' in text
