"""1688 product intake for the admin dashboard: manual cost/title plus a best-effort plain fetch of the product page.

No login, captcha solving or anti-bot evasion. When the owner enters the cost, a blocked or failed page fetch is only a
note. Only https *.1688.com URLs are fetched (redirects are re-checked) so the endpoint cannot reach other hosts.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .alibaba1688 import MAX_IMAGES, AlibabaError, _image_url, _number, parse_product_id

MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3
_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*1688\.com$")
_BLOCK_MARKERS = ("punish", "_____tmd_____", "x5secdata", "nocaptcha", "nc_1_wrapper", "baxia-dialog", "滑动验证",
                  "请拖动下方滑块", "访问被拒绝", "login.1688.com", "login.taobao.com", "passport.1688.com")
_IMAGE = re.compile(r"(?:https?:)?//(?:cbu01|img)\.alicdn\.com/img/ibank/[^\"'\s\\)<>]+?\.(?:jpe?g|png|webp)", re.IGNORECASE)
_CATEGORY = re.compile(r"\"(?:leafCategoryName|categoryName|postCategoryName)\"\s*:\s*\"([^\"\\]{1,40})\"")
_PRICE = re.compile(r"\"(?:price|beginPrice|discountPrice|skuPrice|consignPrice|retailPrice)\"\s*:\s*\"?(\d{1,6}(?:\.\d{1,2})?)")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml", "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8"}
BLOCKED_MESSAGE = "1688 がページの読み取りを止めました（ログイン・ボット確認の画面になりました）。原価（元）を入力すると計算できます。"


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if re.fullmatch(r"\d{6,20}", value):
        return f"https://detail.1688.com/offer/{value}.html"
    parts = urlsplit(value if "://" in value else "https://" + value)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not _HOST.match(host) or parts.port not in (None, 443, 80):
        raise AlibabaError("1688 の商品 URL（detail.1688.com など）か商品 ID を入れてください。", 422, "invalid_product")
    return f"https://{host}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


def _blocked(url: str, text: str) -> bool:
    lowered = (url + " " + text[:200000]).lower()
    return any(marker.lower() in lowered for marker in _BLOCK_MARKERS)


def fetch_page(url: str, session: Any = None, timeout: float = 15) -> tuple[str, str]:
    """GET one 1688 page (no cookies, no retries); returns (final_url, html) or raises page_blocked / page_unavailable."""
    http = session or requests
    current = normalize_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            response = http.get(current, headers=_HEADERS, timeout=timeout, allow_redirects=False, stream=True)
        except requests.RequestException:
            raise AlibabaError("1688 のページに接続できませんでした。原価（元）を入力すると計算できます。", 502, "page_unavailable") from None
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(current, response.headers.get("Location", ""))
                if _blocked(target, ""):
                    raise AlibabaError(BLOCKED_MESSAGE, 422, "page_blocked")
                current = normalize_url(target)
                continue
            if response.status_code in (403, 429) or response.status_code >= 500:
                raise AlibabaError(BLOCKED_MESSAGE if response.status_code in (403, 429) else
                                   "1688 のページを読み取れませんでした。原価（元）を入力すると計算できます。",
                                   422 if response.status_code in (403, 429) else 502,
                                   "page_blocked" if response.status_code in (403, 429) else "page_unavailable")
            if response.status_code >= 400:
                raise AlibabaError("その商品ページは 1688 にありません。", 404, "product_not_found")
            body = b""
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    break
            text = body[:MAX_PAGE_BYTES].decode(response.encoding or "utf-8", errors="replace")
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if _blocked(current, text):
            raise AlibabaError(BLOCKED_MESSAGE, 422, "page_blocked")
        return current, text
    raise AlibabaError("1688 のリダイレクトが多すぎます。原価（元）を入力すると計算できます。", 502, "page_unavailable")


def _meta(page: str, name: str) -> str:
    match = re.search(rf"<meta[^>]+(?:property|name)=[\"']{re.escape(name)}[\"'][^>]*content=[\"']([^\"']*)", page, re.IGNORECASE) or \
        re.search(rf"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']{re.escape(name)}[\"']", page, re.IGNORECASE)
    return html_lib.unescape(match.group(1)).strip() if match else ""


def page_text(page: str, limit: int = 8000) -> str:
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", page, flags=re.IGNORECASE | re.DOTALL)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def parse_page(page: str, url: str) -> dict[str, Any]:
    """Heuristic product fields from a 1688 detail page (title, alicdn images, RMB prices)."""
    title = _meta(page, "og:title")
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
        title = html_lib.unescape(match.group(1)).strip() if match else ""
    title = re.sub(r"\s*[-_|｜]\s*(?:阿里巴巴|1688\.com|1688).*$", "", title)[:200]
    images: list[str] = []
    for raw in [_meta(page, "og:image"), *_IMAGE.findall(page)]:
        clean = _image_url(re.sub(r"(\.(?:jpe?g|png|webp))_[^/]*$", r"\1", raw.replace("\\/", "/"), flags=re.IGNORECASE)) if raw else None
        if clean and clean not in images:
            images.append(clean)
    prices = sorted({p for p in (_number(v) for v in _PRICE.findall(page)) if p and p > 0})
    try:
        product_id = parse_product_id(url)
    except AlibabaError:
        product_id = ""
    category = _CATEGORY.search(page)
    return {"product_id": product_id, "url": url if product_id else None, "title": title,
            "category": html_lib.unescape(category.group(1)).strip() if category else "",
            "images": images[:MAX_IMAGES], "price_ranges": [], "skus": [], "variants": [],
            "min_price_cny": prices[0] if prices else None, "max_price_cny": prices[-1] if prices else None, "unit": "",
            "attributes": [], "supplier": "", "description_excerpt": _meta(page, "description")[:600]}


def intake(url: str, cost_cny: float | None = None, title: str = "", category: str = "", session: Any = None) -> dict[str, Any]:
    """URL (+ manual cost / title / category) → product dict used by the reviewer, pricing and shop listing."""
    target = normalize_url(url)
    notes: list[str] = []
    try:
        final, page = fetch_page(target, session=session)
        product = parse_page(page, final)
        notes.append("商品ページを読み取りました。")
    except AlibabaError as exc:
        if cost_cny is None or exc.code not in ("page_blocked", "page_unavailable", "product_not_found"):
            raise
        product = parse_page("", target)
        notes.append("1688 のページは読めなかったため、入力した値で計算しました。")
    if not product["product_id"]:
        try:
            product["product_id"] = parse_product_id(target)
        except AlibabaError:
            raise AlibabaError("1688 の商品 URL（/offer/<数字>.html）か商品 ID を入れてください。", 422, "invalid_product") from None
    product["url"] = f"https://detail.1688.com/offer/{product['product_id']}.html"
    if title.strip():
        product["title"] = title.strip()[:200]
    if category.strip():
        product["category"] = category.strip()[:80]
    if cost_cny is not None:
        product["min_price_cny"] = product["max_price_cny"] = round(float(cost_cny), 2)
        notes.append("原価は入力した値を使いました。")
    product["title_missing"] = not product["title"]
    if product["title_missing"]:
        product["title"] = f"1688 商品 {product['product_id']}"
        notes.append("商品名が読めなかったため仮の名前です。登録前に商品名を入れてください。")
    product["notes"] = notes
    return product
