import base64
import io

import pytest
from PIL import Image

from karry_agent import alibaba1688 as ali
from karry_agent import product_images as pi
from karry_agent import shop_store


def jpeg(color=(200, 40, 40), size=(400, 300), fmt='JPEG'):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, fmt)
    return out.getvalue()


def data_url(body, mime='image/jpeg'):
    return f'data:{mime};base64,' + base64.b64encode(body).decode()


class Store:
    def __init__(self):
        self.saved = []

    def save_image(self, data):
        self.saved.append(data)
        return f'/api/shop/images/{len(self.saved):032x}.jpg'


class Reply:
    def __init__(self, status=200, body=b''):
        self.status_code, self.body, self.closed = status, body, False

    def iter_content(self, size):
        yield self.body

    def close(self):
        self.closed = True


class Http:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.reply


def test_upload_and_page_photos_are_enhanced_and_put_first():
    store, edits = Store(), []
    product = {'title': '陶瓷杯', 'images': ['https://cbu01.alicdn.com/img/ibank/a.jpg', 'https://cbu01.alicdn.com/img/ibank/b.jpg',
                                            'https://evil.example/x.jpg']}
    http = Http(Reply(200, jpeg((10, 120, 200))))

    def editor(photo, title):
        edits.append((photo[:2], title))
        return jpeg((250, 250, 250))
    info = pi.prepare(product, [data_url(jpeg(), 'image/jpeg')], True, store, editor=editor, session=http)
    assert info == {'engine': 'gemini', 'sources': 3, 'enhanced': 3}
    assert edits == [(b'\xff\xd8', '陶瓷杯')] * 3 and len(store.saved) == 3
    assert product['images'][:3] == [f'/api/shop/images/{i:032x}.jpg' for i in (1, 2, 3)]
    assert 'https://evil.example/x.jpg' not in product['images'] and len(product['images']) == 3
    assert [url for url, _ in http.calls] == product_images_page(product, http)
    assert all(kw['allow_redirects'] is False for _, kw in http.calls)


def product_images_page(product, http):
    return ['https://cbu01.alicdn.com/img/ibank/a.jpg', 'https://cbu01.alicdn.com/img/ibank/b.jpg']


def test_gemini_failure_keeps_the_original_photo_and_disabled_enhance_downloads_nothing():
    store = Store()
    product = {'title': 'x', 'images': ['https://cbu01.alicdn.com/img/ibank/a.jpg']}
    info = pi.prepare(product, [data_url(jpeg())], True, store, editor=lambda photo, title: None, session=Http(Reply(404)))
    assert info == {'engine': 'gemini', 'sources': 1, 'enhanced': 0} and len(store.saved) == 1
    assert product['images'] == ['/api/shop/images/' + f'{1:032x}' + '.jpg', 'https://cbu01.alicdn.com/img/ibank/a.jpg']
    http, store = Http(Reply(200, jpeg())), Store()
    product = {'title': 'x', 'images': ['https://cbu01.alicdn.com/img/ibank/a.jpg']}
    info = pi.prepare(product, [], False, store, editor=lambda photo, title: pytest.fail('no Gemini'), session=http)
    assert info == {'engine': None, 'sources': 0, 'enhanced': 0} and http.calls == [] and product['images'] == ['https://cbu01.alicdn.com/img/ibank/a.jpg']


def test_photo_and_download_validation():
    for bad, code in (('data:image/png;base64,***', 'invalid_photo'), (data_url(b'not an image'), 'invalid_photo')):
        with pytest.raises(ali.AlibabaError) as exc:
            pi.decode_photo(bad)
        assert exc.value.code == code
    assert pi.decode_photo(data_url(jpeg(size=(3000, 2000), fmt='PNG'), 'image/png'))[:2] == b'\xff\xd8'
    assert pi.download('https://evil.example/a.jpg', Http(Reply(200, jpeg()))) is None
    assert pi.download('http://cbu01.alicdn.com/a.jpg', Http(Reply(200, jpeg()))) is None
    assert pi.download('https://cbu01.alicdn.com/a.jpg', Http(Reply(200, b'x' * (pi.MAX_PHOTO_BYTES + 1)))) is None
    assert pi.download('https://cbu01.alicdn.com/a.jpg', Http(Reply(200, b'html'))) is None
    noisy = Image.effect_noise((1600, 1600), 100).convert('RGB')
    out = io.BytesIO()
    noisy.save(out, 'JPEG', quality=100)
    assert len(pi.shop_jpeg(out.getvalue(), limit=200_000)) <= 200_000


def test_gemini_enhance_sends_the_photo_with_a_keep_the_product_prompt(monkeypatch):
    from karry_agent import image_generation as ig
    calls = []

    class Models:
        def generate_content(self, **kw):
            calls.append(kw)
            inline = type('I', (), {'data': jpeg((255, 255, 255)), 'mime_type': 'image/jpeg'})()
            part = type('P', (), {'inline_data': inline})()
            return type('R', (), {'candidates': [type('C', (), {'content': type('X', (), {'parts': [part]})()})()]})()
    monkeypatch.setattr(ig, '_client', lambda: type('Cl', (), {'models': Models()})())
    assert pi.gemini_enhance(jpeg(), '陶瓷杯')[:2] == b'\xff\xd8'
    photo, prompt = calls[0]['contents']
    assert photo.inline_data.mime_type == 'image/jpeg' and 'exactly the same' in prompt and '陶瓷杯' in prompt
    assert calls[0]['config'].image_config.aspect_ratio == '1:1'
    monkeypatch.setattr(ig, '_client', lambda: (_ for _ in ()).throw(ValueError('no key')))
    assert pi.gemini_enhance(jpeg(), 'x') is None


def test_shop_images_round_trip_in_memory_and_durable(monkeypatch):
    monkeypatch.setattr(shop_store.task_persistence, 'mode', lambda: 'memory')
    shop_store._IMAGES.clear()
    url = shop_store.ShopStore().save_image(b'\xff\xd8jpeg')
    assert url.startswith('/api/shop/images/') and shop_store.ShopStore().image(url.split('/')[-1][:-4]) == b'\xff\xd8jpeg'
    docs = {}

    class Repo:
        prefix = 'p/'

        def _cas(self, key, default, apply):
            docs[key] = apply(dict(docs.get(key, default)))

        def _read(self, key):
            return docs.get(key), None
    monkeypatch.setattr(shop_store.task_persistence, 'mode', lambda: 's3')
    monkeypatch.setattr(shop_store.task_persistence, 'repository', lambda: Repo())
    store = shop_store.ShopStore()
    url = store.save_image(b'\xff\xd8durable')
    assert store.image(url.split('/')[-1][:-4]) == b'\xff\xd8durable' and list(docs) == [f"p/shop/images/{url.split('/')[-1][:-4]}.json"]
