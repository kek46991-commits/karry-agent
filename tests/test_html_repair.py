from karry_agent.html_repair import (
    close_truncated_html,
    fallback_page,
    is_complete,
    page_subject,
)


def test_closes_open_elements_in_order():
    repaired = close_truncated_html("<!doctype html><html><body><div><ul><li>一<li>二")
    assert repaired == "<!doctype html><html><body><div><ul><li>一<li>二</li></li></ul></div></body>\n</html>"
    assert is_complete(repaired)


def test_drops_partial_tag_and_comment():
    assert close_truncated_html("<html><body><p>本文</p><img src=\"a").endswith("<p>本文</p></body>\n</html>")
    assert close_truncated_html("<html><body><p>本文</p><!-- 途中").endswith("<p>本文</p></body>\n</html>")


def test_closes_unfinished_script_and_wraps_fragment():
    repaired = close_truncated_html("<body><h1>犬舎</h1><script>if (a < b")
    assert repaired.startswith("<!doctype html>\n<html")
    assert repaired.endswith("<script>if (a < b</script></body>\n</html>")


def test_rejects_documents_without_visible_text():
    assert close_truncated_html("<!doctype html><html><head><style>body{") is None
    assert close_truncated_html("```") is None


def test_empty_body_gets_heading_when_requested():
    repaired = close_truncated_html("<!doctype html><html><head><style>body{", empty_body_heading="<犬>")
    assert repaired.endswith("<style>body{</style></head><body><h1>&lt;犬&gt;</h1></body>\n</html>")


def test_fallback_page_is_complete_and_escapes_request():
    assert page_subject("犬のブリーダーのホームページ作成して公開まで持っていって") == "犬のブリーダー"
    assert page_subject("ホームページを作って") == "ホームページ"
    page = fallback_page("<script>x</script>のホームページ")
    assert is_complete(page.strip()) and "<script>x" not in page
