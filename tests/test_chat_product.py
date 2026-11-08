import pytest
from fastapi.testclient import TestClient

import app as web
from karry_agent import alibaba1688 as ali
from karry_agent import alibaba_api, chat_product, shop_store
from tests.test_chat_video import AUTH, EchoAgent


def test_fields_are_read_from_the_message():
    fields = chat_product.parse_fields("商品名: 木製ブックスタンド 原価 25元\nhttps://detail.1688.com/offer/123456789.html カテゴリ: インテリア")
    assert fields == {"product": "https://detail.1688.com/offer/123456789.html", "cost_cny": 25.0,
                      "title": "木製ブックスタンド", "category": "インテリア"}
    assert chat_product.parse_fields("この写真を出品して")["cost_cny"] is None
    assert chat_product.parse_fields("38.5RMB")["cost_cny"] == 38.5
    rakumart = "https://www.rakumart.com/ProductDetails?goods_id=573741401425&fromPlatform=1688"
    assert chat_product.parse_fields(f"{rakumart} を出品")["product"] == rakumart


@pytest.mark.parametrize("text", ["出品して", "下書きに保存", "商品写真です", "1688 の商品"])
def test_product_wording_is_detected(text):
    assert chat_product.is_product_request(text)


def test_photo_only_draft_without_url_is_polished_and_priced():
    seen = []

    def pipeline(product, photos, enhance, store):
        seen.append((photos, enhance))
        product["images"] = ["/api/shop/images/" + "a" * 32 + ".jpg"]
        product["image_processing"] = {"engine": "gemini", "sources": 1, "enhanced": 1}

    review = {"verdict": "SAFE", "engine": "rules", "checks": {}}
    router = alibaba_api.build_router(lambda: None, reviewer=lambda p: review, image_pipeline=pipeline,
                                      audit=alibaba_api.AuditLog())
    result = chat_product.run(router.create_draft, "商品名: 木製ブックスタンド 原価 25元",
                              [{"data": b"jpeg-bytes", "mime_type": "image/jpeg"}])
    assert result.status == "completed" and seen == [([b"jpeg-bytes"], True)]
    assert "1/1 枚" in result.text and "2,489 円" in result.text and "SAFE" in result.text
    draft_id = result.trace[0].observation["draft_id"]
    assert chat_product.draft_marker(draft_id) in result.text
    draft = shop_store.ShopStore().draft(draft_id)
    assert draft["product"]["product_id"].startswith("photo-") and draft["product"]["url"] is None
    assert not draft["product"]["title_missing"] and draft["pricing"]["selling_price_jpy"] == 2489
    shop_store.ShopStore().delete_draft(draft_id)


def test_missing_photo_and_errors_are_reported():
    assert chat_product.run(lambda *a, **k: {}, "出品して", []).status == "failed"

    def boom(*_, **__):
        raise ali.AlibabaError("写真の形式を読めませんでした（JPG / PNG / WEBP）。", 422, "invalid_photo")

    result = chat_product.run(boom, "出品", [{"data": b"x", "mime_type": "image/png"}])
    assert result.status == "failed" and "写真の形式" in result.text


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    for name in ["_RUNS", "_SESSIONS", "_PRIVATE_SESSIONS", "_PENDING_CALENDAR", "_SESSION_META", "_SESSION_LOCKS", "_SESSION_APPROVALS"]:
        monkeypatch.setattr(web, name, {})
    agent, drafts = EchoAgent(), []

    def create(request, photos, default_cost=None):
        drafts.append((request.title, request.cost_cny, photos))
        return {"draft": {"id": "d" * 32}, "product": {"image_processing": {"sources": 1, "enhanced": 1}},
                "review": {"verdict": "SAFE"}, "pricing": {"selling_price_jpy": 2489}}

    monkeypatch.setattr(web._ALIBABA_ROUTER, "create_draft", create)
    monkeypatch.setattr(web._ALIBABA_ROUTER, "publish_draft",
                        lambda draft_id, via="admin": {"published": False, "listing": None, "reason": "テスト"})
    monkeypatch.setattr(web.image_attachments.AttachmentService, "resolve",
                        lambda self, selected, document=None: [{"data": b"jpeg", "mime_type": "image/jpeg"} for _ in selected])

    def no_model():
        raise AssertionError("the model must not be built for a product draft")

    web.app.dependency_overrides[web.get_agent] = no_model
    client = TestClient(web.app)
    sid = client.post("/api/sessions", json={"title": "shop"}, headers=AUTH).json()["session_id"]
    yield client, sid, agent, drafts
    web.app.dependency_overrides.pop(web.get_agent, None)


def test_chat_switch_saves_a_draft_without_calling_the_model(chat):
    client, sid, _, drafts = chat
    body = client.post("/api/chat", json={"prompt": "商品名: マグカップ 原価 25元", "session_id": sid, "image_ids": ["a" * 32],
                                          "product_draft": True}, headers=AUTH).json()
    assert body["status"] == "completed" and chat_product.draft_marker("d" * 32) in body["text"]
    assert drafts == [("マグカップ", 25.0, [b"jpeg"])] and body["trace"][0]["action"] == chat_product.ACTION


def test_product_wording_with_a_photo_also_saves_a_draft(chat):
    client, sid, _, drafts = chat
    body = client.post("/api/chat", json={"prompt": "この写真を出品して", "session_id": sid, "image_ids": ["b" * 32]}, headers=AUTH).json()
    assert body["status"] == "completed" and len(drafts) == 1


def test_ordinary_photo_chat_still_goes_to_the_agent(chat):
    client, sid, agent, drafts = chat
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    body = client.post("/api/chat", json={"prompt": "これは何？", "session_id": sid, "image_ids": ["c" * 32]}, headers=AUTH).json()
    assert drafts == [] and body["text"].startswith("Answer")


def test_evaluate_still_requires_a_url(monkeypatch):
    monkeypatch.setenv("KARRY_API_TOKEN", "synthetic-owner")
    response = TestClient(web.app).post("/api/alibaba/evaluate", json={"cost_cny": 25}, headers=AUTH)
    assert response.status_code == 422 and response.json()["code"] == "invalid_product"


def _router(review_verdict="SAFE", namer=None, intake=None):
    def pipeline(product, photos, enhance, store):
        product["images"] = [f"/api/shop/images/{i:032x}.jpg" for i in range(len(photos))]
        product["image_processing"] = {"engine": "gemini", "sources": len(photos), "enhanced": len(photos)}

    review = {"verdict": review_verdict, "engine": "rules", "checks": {}}
    return alibaba_api.build_router(lambda: None, reviewer=lambda p: review, image_pipeline=pipeline, intake=intake,
                                    audit=alibaba_api.AuditLog(), namer=namer or (lambda photos: None))


def _jpeg_bytes():
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 80, 40)).save(out, "JPEG")
    return out.getvalue()


def _cleanup(store, result):
    for key, remove in (("listing_id", store.unpublish), ("draft_id", store.delete_draft)):
        if result.trace[0].observation.get(key):
            remove(result.trace[0].observation[key])


def test_rakumart_2c_url_is_recognised_in_chat():
    url = "https://www.rakumart-2c.com/#/details1688?iid=573741401425"
    assert chat_product.parse_fields(f"{url} これを出品")["product"] == url


def test_photos_and_rakumart_url_are_named_by_gemini_and_published_when_safe(monkeypatch):
    monkeypatch.setenv("KARRY_DEFAULT_COST_CNY", "30")
    seen = {}

    def intake(url, cost, title, category):
        seen["intake"] = (url, cost)
        if cost is None:
            raise ali.AlibabaError("blocked", 502, "page_blocked")
        return {"product_id": "573741401425", "url": "https://detail.1688.com/offer/573741401425.html", "title": "1688 商品 573741401425",
                "title_missing": True, "category": "", "min_price_cny": cost, "max_price_cny": cost, "images": [], "notes": []}

    def namer(photos):
        seen["named"] = len(photos)
        return {"title": "折りたたみシリコンカップ 携帯用", "category": "キッチン用品"}

    router = _router(namer=namer, intake=intake)
    photos = [{"data": _jpeg_bytes(), "mime_type": "image/jpeg"} for _ in range(5)]
    result = chat_product.run(router.create_draft, "https://www.rakumart-2c.com/#/details1688?iid=573741401425",
                              photos, publish=router.publish_draft)
    store = shop_store.ShopStore()
    try:
        obs = result.trace[0].observation
        assert result.status == "completed" and obs["published"] and obs["draft_id"] is None
        assert seen["named"] == 3 and seen["intake"] == ("https://www.rakumart-2c.com/#/details1688?iid=573741401425", 30.0)
        listing = next(p for p in store.products() if p["id"] == obs["listing_id"])
        assert listing["title"] == "折りたたみシリコンカップ 携帯用" and listing["category"] == "キッチン用品"
        assert len(listing["images"]) == 5 and listing["status"] == "published"
        assert listing["price_jpy"] == int(ali.price_product(30.0)["selling_price_jpy"])
        assert "ショップに掲載しました" in result.text and "自動掲載完了" in result.text and "デフォルト原価" in result.text
        assert f"{chat_product.LISTING_MARKER}{listing['id']}" in result.text
        log = router.audit.list()[0]
        assert log["action"] == "publish" and log["status"] == "ok" and log["via"] == "chat"
    finally:
        _cleanup(store, result)


def test_reject_or_unpriced_items_stay_as_private_drafts(monkeypatch):
    monkeypatch.delenv("KARRY_DEFAULT_COST_CNY", raising=False)
    store = shop_store.ShopStore()
    photo = [{"data": b"jpeg", "mime_type": "image/jpeg"}]
    rejected_router = _router("REJECT")
    rejected = chat_product.run(rejected_router.create_draft, "商品名: ブランド風バッグ 原価 25元", photo,
                                publish=rejected_router.publish_draft)
    router = _router(namer=lambda photos: {"title": "木製ブックスタンド", "category": "インテリア"})
    unpriced = chat_product.run(router.create_draft, "この写真を出品して", photo, publish=router.publish_draft)
    try:
        for result, reason in ((rejected, "REJECT"), (unpriced, "原価")):
            obs = result.trace[0].observation
            assert result.status == "completed" and not obs["published"] and obs["draft_id"]
            assert "自動掲載しなかった理由" in result.text and reason in result.text
        assert "木製ブックスタンド" in unpriced.text
    finally:
        _cleanup(store, rejected)
        _cleanup(store, unpriced)


def test_auto_publish_can_be_turned_off(chat, monkeypatch):
    client, sid, _, drafts = chat
    published = []
    monkeypatch.setattr(web._ALIBABA_ROUTER, "publish_draft", lambda draft_id, via="admin": published.append(draft_id) or
                        {"published": False, "listing": None, "reason": "x"})
    client.post("/api/chat", json={"prompt": "出品", "session_id": sid, "image_ids": ["a" * 32], "product_draft": True}, headers=AUTH)
    monkeypatch.setenv("KARRY_CHAT_AUTO_PUBLISH", "0")
    client.post("/api/chat", json={"prompt": "出品", "session_id": sid, "image_ids": ["b" * 32], "product_draft": True}, headers=AUTH)
    assert published == ["d" * 32] and len(drafts) == 2


def test_chat_accepts_five_photos(chat):
    client, sid, _, drafts = chat
    response = client.post("/api/chat", json={"prompt": "出品", "session_id": sid, "image_ids": [c * 32 for c in "abcde"],
                                              "product_draft": True}, headers=AUTH)
    assert response.status_code == 200 and len(drafts[0][2]) == 5
    too_many = client.post("/api/chat", json={"prompt": "出品", "session_id": sid, "image_ids": [c * 32 for c in "abcdef"]},
                           headers=AUTH)
    assert too_many.status_code == 422
