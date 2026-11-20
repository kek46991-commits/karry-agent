"""Real assets for generated showroom / metaverse HTML.

* Chat photos sent with the 「商品として登録」 switch OFF are stored as public shop images (R2/S3 when configured)
  so the agent can embed their HTTPS URLs as signboard textures, background panoramas or product-modal images.
* ``shop_product_assets`` resolves a shop product (published or draft) by ID or name to its real image URLs,
  price and purchase page.
* ``apply_branding`` replaces direct-import wording in generated HTML with the shop's standard label.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

BRAND_LABEL = "ラクマート経由 Keli Shop 正規取扱品"
_DIRECT_IMPORT = re.compile(r"(?:アリババ|阿里巴巴|Alibaba|1688)\s*(?:から(?:の)?)?\s*直(?:接)?輸入(?:品)?", re.IGNORECASE)
_SITE_REQUEST = re.compile(
    r"html|ホームページ|サイト|ウェブ|web|ランディング|\blp\b|メタバース|metaverse|3d|ショールーム|showroom|ページ|看板|背景|パノラマ",
    re.IGNORECASE,
)
_HEX32 = re.compile(r"[0-9a-f]{32}")
MAX_RESULTS = 5
MAX_QUERY = 100


def apply_branding(html: str) -> str:
    return _DIRECT_IMPORT.sub(BRAND_LABEL, html) if isinstance(html, str) else html


def is_site_request(text: str) -> bool:
    return bool(_SITE_REQUEST.search(text or ""))


def _base_url() -> str:
    from .instant_website import public_base_url
    return public_base_url()


def _absolute(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("/api/shop/images/"):
        return _base_url() + url
    return url if url.startswith("https://") else None


def publish_attachment_images(images: list[dict], store: Any = None) -> list[str]:
    """Store chat photos as public shop JPEGs and return their absolute HTTPS URLs."""
    from . import product_images, shop_store
    store = store or shop_store.ShopStore()
    return [_absolute(store.save_image(product_images.shop_jpeg(image["data"]))) for image in images]


def asset_prompt(urls: list[str]) -> str:
    lines = "\n".join(f"- 画像{i}: {url}" for i, url in enumerate(urls, 1))
    return ("\n\n[添付画像の公開URL（HTML素材）]\n" + lines +
            "\nHTML・3D空間を作る場合は、これらのURLを<img src>やテクスチャ（看板・背景パノラマ・商品詳細モーダル画像）"
            "としてそのまま埋め込んでください。ほかの画像URLを捏造しないでください。")


def _product_view(item: dict[str, Any]) -> dict[str, Any]:
    images = [u for u in map(_absolute, item.get("images") or []) if u][:12]
    return {"id": item["id"], "status": "published", "title": item.get("title") or "", "category": item.get("category"),
            "price_jpy": item.get("price_jpy"), "image_urls": images,
            "product_url": f"{_base_url()}/products/{item['id']}", "brand_label": BRAND_LABEL}


def _draft_view(draft: dict[str, Any]) -> dict[str, Any]:
    product = draft.get("product") or {}
    images = [u for u in map(_absolute, product.get("images") or []) if u][:12]
    price = (draft.get("pricing") or {}).get("selling_price_jpy")
    return {"id": draft["id"], "status": "draft", "title": product.get("title") or "", "category": product.get("category"),
            "price_jpy": int(price) if price else None, "image_urls": images, "product_url": None,
            "brand_label": BRAND_LABEL, "note": "下書きのため購入ページはまだありません。購入ボタンは「準備中」と表示してください。"}


def shop_product_assets(query: str, store: Any = None) -> dict[str, Any]:
    """Look up shop products (published first, then drafts) by exact ID or by name."""
    from . import shop_store
    query = (query or "").strip()
    if not query or len(query) > MAX_QUERY:
        return {"status": "error", "error": "商品IDまたは商品名を100文字以内で指定してください。"}
    store = store or shop_store.ShopStore()
    published = [p for p in store.products() if p.get("status") == "published"]
    drafts = store.drafts()
    if _HEX32.fullmatch(query.lower()):
        key = query.lower()
        found = [_product_view(p) for p in published if p["id"] == key] + [_draft_view(d) for d in drafts if d["id"] == key]
    else:
        needle = query.casefold()
        found = ([_product_view(p) for p in published if needle in (p.get("title") or "").casefold()]
                 + [_draft_view(d) for d in drafts if needle in ((d.get("product") or {}).get("title") or "").casefold()])
    if not found:
        return {"status": "not_found", "query": query, "products": []}
    return {"status": "success", "query": query, "products": found[:MAX_RESULTS],
            "usage": "展示ボードと商品詳細モーダルに image_urls・price_jpy・title をそのまま使い、購入ボタンは product_url への"
                     "通常リンク（target属性なし）にしてください。表記は brand_label を使ってください。"}


_QUOTED = re.compile(r"[「『\"“]([^「」『』\"“”]{2,60})[」』\"”]")
_HEX32_ANY = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")


def products_for_request(text: str, store: Any = None) -> list[dict[str, Any]]:
    """Shop products referenced by a request: 32-hex IDs, quoted names, or full product titles in the text."""
    from . import shop_store
    text = text or ""
    store = store or shop_store.ShopStore()
    queries = _HEX32_ANY.findall(text.lower()) + _QUOTED.findall(text)
    titles = [p.get("title") or "" for p in store.products() if p.get("status") == "published"]
    titles += [(d.get("product") or {}).get("title") or "" for d in store.drafts()]
    queries += [t for t in titles if len(t) >= 4 and t in text]
    found: dict[str, dict[str, Any]] = {}
    for query in dict.fromkeys(queries):
        for item in shop_product_assets(query, store).get("products", []):
            found.setdefault(item["id"], item)
    return list(found.values())[:MAX_RESULTS]


def website_context(request: str, store: Any = None) -> str:
    """Rules (and real product data when referenced) appended to the one-shot website generation prompt."""
    import json
    rules = (f"\n\n[ショップ表記ルール] 商品の出自表記は「{BRAND_LABEL}」とし、「アリババ直輸入」「1688直輸入」等は使わないでください。"
             "画像・価格・購入リンクは、下記データや依頼文にあるURL以外を捏造しないでください。")
    try:
        products = products_for_request(request, store)
    except Exception:
        logger.warning("shop product lookup failed for website context", exc_info=True)
        products = []
    if not products:
        return rules
    data = json.dumps(products, ensure_ascii=False)
    return (rules + "\n[実在の商品データ（展示ボード・商品詳細モーダルに使用）]\n" + data +
            "\nimage_urlsを展示ボードと商品詳細モーダルの<img src>に、title・price_jpy（円表記）をそのまま使ってください。"
            "購入ボタンはproduct_urlへの通常の<a href>リンク（target属性なし）にし、product_urlがnullの商品は「準備中」と表示してください。")
