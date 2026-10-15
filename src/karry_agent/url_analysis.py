"""Single-page extraction: no browser, crawl, LLM or authenticated requests."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import subprocess
import sys
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import urllib3
from bs4 import BeautifulSoup

from .self_healing import healing_engine

MAX_HTML_BYTES = 1_048_576
MAX_TEXT_CHARS = 3000
MAX_REDIRECTS = 3
FETCH_TIMEOUT = 10.0
_DNS_SLOTS = BoundedSemaphore(4)


class URLFetchError(ValueError):
    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        self.code, self.message, self.http_status = code, message, http_status
        super().__init__(message)


# 404・アクセス拒否・存在しないドメインは、再試行せずユーザーにURL確認を促す。
UNREACHABLE_ERRORS = frozenset({"url_not_found", "url_access_denied", "dns_error"})
UNREACHABLE_MESSAGE = "指定されたURLが存在しないかアクセスできません。URLを確認してください。"
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_URL_RE = re.compile(r"(?<![A-Za-z0-9@./])h?ttps?://[^\s<>\"'`]+", re.I)
# ひらがな・和文記号・全角記号が来たらURLの終わりとみなす（漢字・カタカナのパスは残す）。
_URL_STOP_RE = re.compile("[\u3000-\u303f\u3040-\u309f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65…⋯]")
_CONTINUATION_RE = re.compile(r"[ \t]*\r?\n[ \t]*([A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)"
                              "(?=[\\s\u3000-\u303f\u3040-\u309f\uff01-\uff65]|\\Z)")
_JOIN_MARKS = "/-_.?=&#%~+"


def _tidy_url(raw: str) -> tuple[str, int]:
    """整形後URLと、元の文字列のうちURLとして使った長さ。"""
    url = raw
    while True:
        before = url
        url = url.rstrip(",;:*'\"")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        while url.endswith("]") and url.count("]") > url.count("["):
            url = url[:-1]
        if url == before:
            break
    used = len(url)
    if url[:4].lower() in {"ttps", "ttp:"}:
        url = "h" + url
    return (url if re.match(r"https?://[^/?#\s]", url, re.I) else ""), used


def _url_spans(text: str) -> list[tuple[int, int, str]]:
    """URLの位置と整形後URL。改行で折り返されたURLは続きの行と結合する。"""
    spans: list[tuple[int, int, str]] = []
    for match in _URL_RE.finditer(text):
        start, raw = match.start(), match.group()
        if spans and start < spans[-1][1]:
            continue
        stop = _URL_STOP_RE.search(raw)
        if stop:
            raw = raw[:stop.start()]
        else:
            position = match.end()
            while continuation := _CONTINUATION_RE.match(text, position):
                token = continuation.group(1)
                if re.match(r"h?ttps?://", token, re.I) or not (
                        raw[-1] in _JOIN_MARKS or token[0] in _JOIN_MARKS or any(c in token for c in "/?=&%#")):
                    break
                raw += token
                position = continuation.end()
            if position != match.end():
                url, used = _tidy_url(raw)
                spans.append((start, position - (len(raw) - used), url))
                continue
        url, used = _tidy_url(raw)
        spans.append((start, start + used, url))
    return [span for span in spans if span[2]]


def extract_urls(text: str) -> list[str]:
    """入力文から有効な形に整えたURLを重複なしで取り出す。"""
    return list(dict.fromkeys(url for _, _, url in _url_spans(text.translate(_ZERO_WIDTH))))


def normalize_urls_in_text(text: str) -> str:
    """折り返し・前後の余分な文字を除いたURLへ置き換え、直後の文と区切る。"""
    text = text.translate(_ZERO_WIDTH)
    for start, end, url in reversed(_url_spans(text)):
        tail = text[end:]
        text = text[:start] + url + (" " if tail and not tail[0].isspace() else "") + tail
    return text


def clean_url(value: Any) -> Any:
    """ツール引数のURLの前後の空白・囲み文字・句読点を除き、www.だけの場合はhttps://を補う。

    途中の空白・改行は結合しない（ヘッダー注入対策として validate_url が拒否する）。
    """
    if not isinstance(value, str):
        return value
    compact = value.translate(_ZERO_WIDTH).strip().strip("<>\"'`「」『』").strip()
    if compact.lower().startswith("www."):
        compact = "https://" + compact
    if re.search(r"\s", compact):
        return compact
    urls = extract_urls(compact)
    return urls[0] if len(urls) == 1 else compact


def _http_failure(status: int) -> URLFetchError:
    if status in {404, 410}:
        return URLFetchError("url_not_found", UNREACHABLE_MESSAGE, status)
    if status in {401, 403, 451}:
        return URLFetchError("url_access_denied", UNREACHABLE_MESSAGE, status)
    return URLFetchError("http_error", f"WebページがHTTP {status}を返しました。", status)


def _unreachable_result(url: str, exc: URLFetchError) -> dict[str, Any]:
    reason = {404: "HTTP 404 Not Found", 410: "HTTP 410 Gone", 401: "HTTP 401 認証が必要",
              403: "HTTP 403 アクセス拒否", 451: "HTTP 451 アクセス制限"}.get(exc.http_status or 0, "ドメインが見つかりません")
    shown = url if isinstance(url, str) and len(url) <= 300 else "（長いURL）"
    return {"status": "needs_user_action", "error": exc.code, "url": url, "http_status": exc.http_status,
            "message": f"{UNREACHABLE_MESSAGE}\n対象: {shown}（{reason}）\n"
                       "URLを修正して送り直すか、ページ本文やスクリーンショットを貼り付けるなど、別の指示をお願いします。"}


def _public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def validate_url(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise URLFetchError("invalid_url", "URLの形式が不正です。")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise URLFetchError("invalid_url", "URLの形式が不正です。") from None
    if parts.scheme not in {"http", "https"} or not host or parts.username is not None or parts.password is not None:
        raise URLFetchError("invalid_url", "認証情報を含まない公開HTTP(S) URLを指定してください。")
    if port != (443 if parts.scheme == "https" else 80):
        raise URLFetchError("blocked_port", "標準のHTTP(S)ポートだけを利用できます。")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "%" in host or "\\" in url:
        raise URLFetchError("blocked_address", "内部ネットワークのURLは取得できません。")
    return parts.scheme, host, port


def _resolve_public(host: str, port: int, remaining: float) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _DNS_SLOTS.acquire(blocking=False):
            raise URLFetchError("dns_busy", "名前解決が混雑しています。後で再試行してください。")
        try:
            process = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("_url_dns.py")), host, str(port)],
                                     capture_output=True, text=True, check=False, timeout=max(0.01, remaining))
            if process.returncode:
                raise URLFetchError("dns_error", "公開ホストの名前解決に失敗しました。")
            addresses = json.loads(process.stdout)
        except subprocess.TimeoutExpired:
            # subprocess.run kills and reaps the child before raising.
            raise URLFetchError("timeout", "URL取得の時間上限に達しました。") from None
        finally:
            _DNS_SLOTS.release()
    else:
        addresses = [host]
    if not addresses or not all(_public_ip(ip) for ip in addresses):
        raise URLFetchError("blocked_address", "内部・非公開ネットワークへの接続を拒否しました。")
    return addresses[0]


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise URLFetchError("timeout", "URL取得の時間上限に達しました。")
    return remaining


def _download_html(url: str) -> tuple[bytes, str, str, int]:
    deadline = time.monotonic() + FETCH_TIMEOUT
    current = url
    for redirects in range(MAX_REDIRECTS + 1):
        scheme, host, port = validate_url(current)
        ip = _resolve_public(host, port, _remaining(deadline))
        parts = urlsplit(current)
        target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            target += "?" + quote(parts.query, safe="%=&/:?@!$'()*+,;~-._")
        authority = "[" + host + "]" if ":" in host else host
        kwargs: dict[str, Any] = {"port": port, "maxsize": 1, "block": True}
        if scheme == "https":
            pool = urllib3.HTTPSConnectionPool(ip, server_hostname=host, assert_hostname=host,
                                               cert_reqs="CERT_REQUIRED", **kwargs)
        else:
            pool = urllib3.HTTPConnectionPool(ip, **kwargs)
        response = None
        try:
            # Connect to the validated numeric IP (no DNS rebinding), preserve TLS
            # certificate verification and Host; never use env proxies/cookies/auth.
            response = pool.urlopen("GET", target, headers={"Host": authority,
                "User-Agent": "Mozilla/5.0 (compatible; KarryAgent/0.1; single-page-analysis)",
                "Accept": "text/html,text/plain;q=0.9", "Accept-Encoding": "identity"},
                timeout=urllib3.Timeout(total=_remaining(deadline), connect=min(3, _remaining(deadline)),
                                        read=min(3, _remaining(deadline))),
                retries=False, redirect=False, preload_content=False, decode_content=False,
                assert_same_host=False)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location or redirects == MAX_REDIRECTS:
                    raise URLFetchError("redirect_limit", "リダイレクトの上限または不正な転送先です。")
                next_url = urljoin(current, location)
                validate_url(next_url)
                if scheme == "https" and urlsplit(next_url).scheme != "https":
                    raise URLFetchError("redirect_downgrade", "HTTPSからHTTPへの転送は取得できません。")
                current = next_url
                continue
            if not 200 <= response.status < 300:
                raise _http_failure(response.status)
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise URLFetchError("unsupported_content", "HTMLまたはテキストのページだけを解析できます。")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise URLFetchError("unsupported_encoding", "圧縮応答はこの軽量取得では処理できません。")
            size = response.headers.get("Content-Length", "")
            if size.isdigit() and int(size) > MAX_HTML_BYTES:
                raise URLFetchError("page_too_large", "ページ容量が1 MiBの上限を超えています。")
            body = bytearray()
            while True:
                remaining = _remaining(deadline)
                # The pool's initial read timeout is not an overall streaming
                # deadline; update the live socket before each read.
                connection = response.connection
                if connection is not None and connection.sock is not None:
                    connection.sock.settimeout(min(3, remaining))
                chunk = response.read1(min(65_536, MAX_HTML_BYTES + 1 - len(body)), decode_content=False)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_HTML_BYTES:
                    raise URLFetchError("page_too_large", "ページ容量が1 MiBの上限を超えています。")
            _remaining(deadline)
            return bytes(body), current, response.headers.get("Content-Type", ""), redirects
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise URLFetchError("redirect_limit", "転送上限です。")


def _compact(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]


def extract_page(body: bytes, url: str, content_type: str, redirects: int = 0) -> dict[str, Any]:
    if content_type.lower().startswith("text/plain"):
        text = body.decode("utf-8", errors="replace")
        if not text.strip():
            return {"status": "needs_user_action", "error": "empty_content", "url": url,
                    "message": "本文が空でした。ページ本文を共有してください。"}
        return {"status": "success", "title": "", "content_preview": _compact(text, MAX_TEXT_CHARS),
                "url": url, "headings": [], "description": "", "structured_data_types": [],
                "truncated": len(_compact(text, MAX_TEXT_CHARS + 1)) > MAX_TEXT_CHARS, "redirects": redirects}
    soup = BeautifulSoup(body, "html.parser")
    title = _compact(soup.title.get_text(" ", strip=True), 200) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = _compact(str(meta.get("content", "")), 300) if meta else ""
    types: list[str] = []
    # Extract schema types only, never return arbitrary executable JSON-LD blobs.
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}, limit=3):
        try:
            data = json.loads((script.string or "")[:20_000])
            nodes = data if isinstance(data, list) else [data]
            if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                nodes += data["@graph"][:10]
            for node in nodes[:10]:
                if isinstance(node, dict):
                    kind = node.get("@type")
                    for item in kind if isinstance(kind, list) else [kind]:
                        if isinstance(item, str) and item not in types:
                            types.append(item[:100])
        except (ValueError, TypeError, RecursionError):
            pass
    for element in list(soup(["script", "style", "nav", "footer", "iframe", "noscript", "template"])):
        if element.parent is not None:
            element.decompose()
    for element in list(soup.select("[hidden], [aria-hidden='true']")):
        if element.parent is not None:
            element.decompose()
    headings = [{"level": h.name, "text": _compact(h.get_text(" ", strip=True), 160)}
                for h in soup.find_all(["h1", "h2", "h3"], limit=10)]
    region = soup.find("main") or soup.find("article") or soup.body or soup
    text = _compact(region.get_text(" ", strip=True), MAX_TEXT_CHARS + 1)
    if not text:
        return {"status": "needs_user_action", "error": "empty_content", "title": title, "url": url,
                "message": "本文を取得できませんでした。JavaScript描画・ログイン・画像中心のページの場合は本文かスクリーンショットを共有してください。"}
    return {"status": "success", "title": title, "description": description, "url": url,
            "content_preview": text[:MAX_TEXT_CHARS], "headings": headings,
            "structured_data_types": types[:10], "truncated": len(text) > MAX_TEXT_CHARS,
            "redirects": redirects, "content_source": "static_html"}


def fetch_and_summarize_url(url: str) -> dict[str, Any]:
    """Extract in one logical fetch; summarization/proposals belong to the final LLM call."""
    started = time.monotonic()
    url = clean_url(url)
    try:
        validate_url(url)
        body, final_url, content_type, redirects = _download_html(url)
        result = extract_page(body, final_url, content_type, redirects)
    except URLFetchError as exc:
        result = (_unreachable_result(url, exc) if exc.code in UNREACHABLE_ERRORS
                  else {"status": "error", "error": exc.code, "message": exc.message, "url": url})
    except (urllib3.exceptions.TimeoutError, socket.timeout):
        result = {"status": "error", "error": "timeout", "message": "URL取得の時間上限に達しました。"}
    except Exception as exc:
        healing = healing_engine.analyze_and_heal(exc, {"source": "url_fetch", "operation": "fetch_and_summarize_url"})
        result = {"status": "error", "error": "fetch_failed", "message": "Webページの取得に失敗しました。", "healing_status": healing}
    return {**result, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}


async def fetch_and_summarize_url_async(url: str) -> dict[str, Any]:
    """Async facade for integrations; the existing synchronous registry uses the function above."""
    return await asyncio.to_thread(fetch_and_summarize_url, url)


def single_analysis_url(prompt: str) -> str | None:
    """Fast path only for a single explicit read/analysis request or a bare URL."""
    text = prompt.translate(_ZERO_WIDTH)
    spans = _url_spans(text)
    if len({found for _, _, found in spans}) != 1:
        return None
    url = spans[0][2]
    remainder = text
    for start, end, _ in reversed(spans):
        remainder = remainder[:start] + " " + remainder[end:]
    remainder = remainder.strip()
    # Reject unrelated/compound actions rather than replacing their semantics.
    phrases = (r"この|こちらの|こちら|以下の|以下|ページ|サイト|ウェブサイト|Webサイト|URL|内容|改善|提案|生成|"
               r"解析|分析|要約|確認|見て|見てみて|読んで|レビュー|お願い|してください|して下さい|してくださいませ|"
               r"して|ください|下さい|ほしい|してほしい|教えて|お願いしたい|お願いします|お願い致します|"
               r"を|の|と|について|から|してもらえますか|してくれる|してくれるか|"
               r"analyze|analyse|summarize|summarise|review|suggestions|suggest|improvements|improve|"
               r"this|the|page|website|url|and|please")
    remainder = re.sub(phrases, "", remainder, flags=re.I)
    remainder = re.sub(r"[\s。、，!！?？:：()（）\[\]【】「」『』`]+", "", remainder)
    return url if not remainder else None
