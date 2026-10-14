"""Close the open tags of an HTML document whose generation stopped part-way."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
})
_COMPLETE_HTML_RE = re.compile(
    r"\A(?:<!doctype\s+html(?:\s[^>]*)?>\s*)?<html\b[^>]*>[\s\S]*</html\s*>\Z", re.IGNORECASE
)
_TRAILING_FENCE_RE = re.compile(r"\s*```\s*\Z")


class _OpenTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.text_chars = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1] not in {"script", "style"}:
            self.text_chars += len(data.strip())


def is_complete(html: str) -> bool:
    return bool(_COMPLETE_HTML_RE.fullmatch(html))


def close_truncated_html(fragment: str, *, empty_body_heading: str | None = None) -> str | None:
    """Return a well-formed document, or None when nothing visible survived.

    With ``empty_body_heading``, a document cut off before any visible text gets a body with that heading
    instead of being rejected."""
    text = _TRAILING_FENCE_RE.sub("", fragment).rstrip()
    if text.rfind("<!--") > text.rfind("-->"):
        text = text[: text.rfind("<!--")].rstrip()
    lowered = text.lower()
    in_raw_text = any(lowered.rfind(f"<{tag}") > lowered.rfind(f"</{tag}") for tag in ("script", "style"))
    last_open, last_close = text.rfind("<"), text.rfind(">")
    if last_open > last_close and not in_raw_text:
        text = text[:last_open].rstrip()
    if not re.search(r"<html\b", text, re.IGNORECASE):
        text = "<!doctype html>\n<html lang=\"ja\">\n" + text
    parser = _OpenTags()
    parser.feed(text)
    closing = "".join(f"</{tag}>" for tag in reversed(parser.stack) if tag != "html")
    if parser.text_chars == 0:
        if empty_body_heading is None:
            return None
        if "body" in parser.stack:
            closing = f"<h1>{html.escape(empty_body_heading)}</h1>" + closing
        else:
            closing += f"<body><h1>{html.escape(empty_body_heading)}</h1></body>"
    repaired = text + closing + "\n</html>"
    return repaired if is_complete(repaired) else None


_SUBJECT_RE = re.compile(
    r"(?:の)?(?:ホームページ|(?:Web|ウェブ)サイト|(?:Web|ウェブ)ページ|サイト|HTML|website|web\s*site|landing\s*page)[\s\S]*\Z",
    re.IGNORECASE,
)


def page_subject(request: str) -> str:
    subject = _SUBJECT_RE.sub("", " ".join(request.split())).strip(" 、。,.!！?？「」『』\"'")
    return subject[:60] or "ホームページ"


def fallback_page(request: str) -> str:
    """A plain, editable page built only from the request text, for when the model produced no usable HTML."""
    subject = html.escape(page_subject(request))
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{subject}</title>
<style>
body{{margin:0;font:16px/1.8 system-ui,sans-serif;color:#2b2b2b;background:#faf8f5}}
header{{background:#5b7f6a;color:#fff;padding:48px 24px;text-align:center}}
main{{max-width:880px;margin:0 auto;padding:24px}}
section{{background:#fff;border-radius:12px;padding:20px 24px;margin:20px 0;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
h1{{margin:0;font-size:clamp(26px,5vw,40px)}}
h2{{color:#5b7f6a;margin-top:0}}
footer{{text-align:center;color:#777;padding:24px}}
</style>
</head>
<body>
<header><h1>{subject}</h1></header>
<main>
<section><h2>私たちについて</h2><p>{subject}のご紹介文をここに入れてください。</p></section>
<section><h2>ご案内</h2><p>サービス・商品・最新情報などをここに入れてください。</p></section>
<section><h2>お問い合わせ</h2><p>連絡先や営業時間をここに入れてください。</p></section>
</main>
<footer>&copy; {subject}</footer>
</body>
</html>
"""
