"""Close the open tags of an HTML document whose generation stopped part-way."""
from __future__ import annotations

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


def close_truncated_html(fragment: str) -> str | None:
    """Return a well-formed document, or None when nothing visible survived."""
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
    if parser.text_chars == 0:
        return None
    closing = "".join(f"</{tag}>" for tag in reversed(parser.stack) if tag != "html")
    repaired = text + closing + "\n</html>"
    return repaired if is_complete(repaired) else None
