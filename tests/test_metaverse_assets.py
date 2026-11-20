import io

from PIL import Image

import app as web
from karry_agent import instant_website, metaverse_assets, shop_store
from karry_agent.tools import build_default_registry
from tests.test_chat_product import chat  # noqa: F401
from tests.test_chat_video import AUTH

BASE = "https://karry-agent-1.onrender.com"


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (40, 30), (200, 10, 10, 128)).save(out, "PNG")
    return out.getvalue()


class FakeStore:
    def __init__(self):
        self.saved = []
        self.items = [{"id": "a" * 32, "status": "published", "title": "北欧マグカップ", "category": "食器",
                       "images": ["/api/shop/images/" + "1" * 32 + ".jpg", "http://insecure/x.jpg"], "price_jpy": 2489}]
        self.draft_items = [{"id": "b" * 32, "updated_at": 1, "product": {"title": "竹のトレー", "images": ["https://cbu01.alicdn.com/t.jpg"]},
                             "pricing": {"selling_price_jpy": 1980}}]

    def save_image(self, data):
        self.saved.append(data)
        return "/api/shop/images/" + "c" * 32 + ".jpg"

    def products(self):
        return self.items

    def drafts(self):
        return self.draft_items


def test_direct_import_wording_is_rebranded():
    html = "<p>アリババ直輸入</p><p>1688直輸入品</p><p>Alibabaから直輸入</p><p>1688 商品</p>"
    out = metaverse_assets.apply_branding(html)
    assert "直輸入" not in out and out.count(metaverse_assets.BRAND_LABEL) == 3 and "1688 商品" in out


def test_instant_website_plan_applies_branding(monkeypatch):
    monkeypatch.setenv("KARRY_WEBSITE_STORAGE", "local")
    plan = instant_website._plan("<html><body>1688直輸入の雑貨</body></html>", "shop")
    assert "ラクマート経由 Keli Shop 正規取扱品の雑貨" in plan["html_content"]


def test_attachments_become_public_jpeg_urls(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)
    store = FakeStore()
    urls = metaverse_assets.publish_attachment_images([{"data": _png(), "mime_type": "image/png"}], store)
    assert urls == [f"{BASE}/api/shop/images/{'c' * 32}.jpg"]
    assert store.saved[0][:2] == b"\xff\xd8"


def test_published_and_draft_products_resolve_to_real_assets(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)
    store = FakeStore()
    by_id = metaverse_assets.shop_product_assets("a" * 32, store)
    product = by_id["products"][0]
    assert product["price_jpy"] == 2489 and product["product_url"] == f"{BASE}/products/{'a' * 32}"
    assert product["image_urls"] == [f"{BASE}/api/shop/images/{'1' * 32}.jpg"]
    assert product["brand_label"] == metaverse_assets.BRAND_LABEL
    draft = metaverse_assets.shop_product_assets("トレー", store)["products"][0]
    assert draft["status"] == "draft" and draft["price_jpy"] == 1980 and draft["product_url"] is None
    assert draft["image_urls"] == ["https://cbu01.alicdn.com/t.jpg"]
    assert metaverse_assets.shop_product_assets("存在しない", store)["status"] == "not_found"
    assert metaverse_assets.shop_product_assets("", store)["status"] == "error"


def test_lookup_reads_the_real_shop_store(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)
    store = shop_store.ShopStore()
    item = store.publish({"source": "1688", "source_product_id": "meta-test", "title": "メタバース展示テスト品",
                          "category": "雑貨", "images": [store.save_image(b"\xff\xd8jpeg")], "price_jpy": 3200})
    try:
        found = metaverse_assets.shop_product_assets("メタバース展示テスト", store)["products"][0]
        assert found["id"] == item["id"] and found["price_jpy"] == 3200
        assert found["image_urls"][0].startswith(f"{BASE}/api/shop/images/")
    finally:
        store.unpublish(item["id"])


def test_tool_is_registered():
    assert "get_shop_product_assets" in build_default_registry().tools


def test_site_request_with_photo_and_switch_off_gets_public_urls(chat, monkeypatch):  # noqa: F811
    client, sid, agent, drafts = chat
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)
    monkeypatch.setattr(metaverse_assets, "publish_attachment_images",
                        lambda images, store=None: [f"{BASE}/api/shop/images/{'d' * 32}.jpg" for _ in images])
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    body = client.post("/api/chat", json={"prompt": "この画像を看板にした3Dメタバースのショールームを作って", "session_id": sid,
                                          "image_ids": ["e" * 32]}, headers=AUTH).json()
    assert drafts == [] and body["text"].startswith("Answer")
    assert f"{BASE}/api/shop/images/{'d' * 32}.jpg" in agent.prompts[-1]


def test_ordinary_photo_question_stays_private(chat, monkeypatch):  # noqa: F811
    client, sid, agent, drafts = chat
    monkeypatch.setattr(metaverse_assets, "publish_attachment_images",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not publish")))
    web.app.dependency_overrides[web.get_agent] = lambda: agent
    body = client.post("/api/chat", json={"prompt": "これは何？", "session_id": sid, "image_ids": ["f" * 32]}, headers=AUTH).json()
    assert drafts == [] and "公開URL" not in agent.prompts[-1] and body["text"].startswith("Answer")


def test_products_are_found_by_id_quoted_name_or_title(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)
    store = FakeStore()
    assert [p["id"] for p in metaverse_assets.products_for_request(f"商品ID {'a' * 32} を展示", store)] == ["a" * 32]
    assert [p["id"] for p in metaverse_assets.products_for_request("「竹のトレー」の3D展示", store)] == ["b" * 32]
    assert [p["id"] for p in metaverse_assets.products_for_request("北欧マグカップと竹のトレーを並べて", store)] == ["a" * 32, "b" * 32]
    assert metaverse_assets.products_for_request("ショールームを作って", store) == []


def test_website_context_carries_rules_and_real_product_data(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", BASE)
    context = metaverse_assets.website_context(f"{'a' * 32} のショールーム", FakeStore())
    assert metaverse_assets.BRAND_LABEL in context and f"{BASE}/products/{'a' * 32}" in context and "2489" in context
    plain = metaverse_assets.website_context("ショールームを作って", FakeStore())
    assert metaverse_assets.BRAND_LABEL in plain and "実在の商品データ" not in plain


def test_one_shot_website_generation_receives_shop_context(monkeypatch):
    from karry_agent.core import AgentCore
    from tests.test_website_creation_core import (
        HTML,
        fake_client,
        model_response,
        registry_with_prepare,
        text_part,
    )
    monkeypatch.setattr(metaverse_assets, "website_context", lambda request, store=None: "\n[SHOP-CONTEXT]")
    registry, _ = registry_with_prepare()
    client, generator = fake_client(model_response(text_part(HTML)))
    AgentCore(client=client, registry=registry).run("3Dショールームのホームページを作成して")
    assert "[SHOP-CONTEXT]" in generator.call_args.kwargs["contents"][0].parts[0].text
