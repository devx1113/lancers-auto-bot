"""Job links, portfolio parsing, page text, the public-address check, web search.   python tests/test_webpages.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lancers_bot import proposals, webpages  # noqa: E402


def test_job_links():
    text = ("現在のサイト https://shonan-kaigan.com/を改修したいです。参考: www.example.co.jp/works。\n"
            "仕様書→https://docs.example.com/spec?id=1）詳細は https://www.lancers.jp/work/detail/1 と"
            "https://shonan-kaigan.com/ を参照。https://a.example.com https://b.example.com https://c.example.com")
    assert webpages.find_urls(text)[:3] == ["https://shonan-kaigan.com/", "https://www.example.co.jp/works", "https://docs.example.com/spec?id=1"]
    assert webpages.job_links({"description": text}) == ["https://shonan-kaigan.com/", "https://www.example.co.jp/works",
                                                         "https://docs.example.com/spec?id=1"]      # no Lancers page, at most 3
    assert webpages.job_links({"description": "URLなし"}) == [] and webpages.job_links({}) == []


def test_parse_entries():
    entries = webpages.parse_entries("https://a.example.com/shop  ECサイト構築（決済連携）\n\n"
                                      "予約システム — https://b.example.com/\nhttps://a.example.com/shop")
    assert entries == [{"url": "https://a.example.com/shop", "note": "ECサイト構築（決済連携）"},
                       {"url": "https://b.example.com/", "note": "予約システム"}]
    assert webpages.parse_entries(entries) == entries          # the list the server sends back
    for bad in ("説明だけでURLなし", "ftp://x.example.com/file"):
        try:
            webpages.parse_entries(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_extract():
    title, text = webpages.extract("""<html><head><title>予約システム | Example</title>
        <meta name="description" content="美容室向けのオンライン予約"><script>var x = 1;</script><style>p{}</style></head>
        <body><nav>メニュー</nav><h1>機能</h1><p>LINE連携   と
        決済</p></body></html>""".encode())
    assert title == "予約システム | Example"
    assert text.startswith("美容室向けのオンライン予約\n") and "LINE連携 と\n決済" in text and "var x" not in text


def test_private_addresses_refused():
    assert not webpages.ALLOW_PRIVATE
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "169.254.169.254", "::1"):
        try:
            webpages.check_host(host)
            raise AssertionError(host)
        except webpages.PageError:
            pass


def test_prompt_block():
    entries = [{"url": "https://a.example.com/", "note": "EC構築"}, {"url": "https://b.example.com/", "note": ""}]
    pages = {"https://a.example.com/": {"status": "ok", "title": "Shop", "text": "商品一覧と決済"},
             "https://b.example.com/": {"status": "error", "error": "HTTP 404"}}
    block = webpages.portfolio_block(entries, pages)
    assert "1. https://a.example.com/\n本人による説明: EC構築\nページのタイトル: Shop\nページの内容（抜粋）:\n商品一覧と決済" in block
    assert "2. https://b.example.com/\n（ページを取得できませんでした" in block
    assert webpages.portfolio_block([], {}) == ""
    block = webpages.job_links_block(["https://a.example.com/", "https://b.example.com/"], pages)
    assert "■ https://a.example.com/\nページのタイトル: Shop\nページの内容（抜粋）:\n商品一覧と決済" in block
    assert "■ https://b.example.com/\n（取得できませんでした: HTTP 404。" in block and webpages.job_links_block([], {}) == ""


def test_web_search_request():
    body = proposals.request_body("x-ai/grok-4.7", [], "high", "fast", web_search=True)
    assert body["plugins"] == [{"id": "web"}] and body["model"] == "x-ai/grok-4.7:nitro"
    assert "plugins" not in proposals.request_body("x-ai/grok-4.7", [])
    assert "Web検索の結果が与えられる" in proposals.rules(True) and "Webの閲覧はできない" in proposals.rules(False)
    assert proposals.clean("詳しくは[公式サイト](https://example.com/a)をご覧ください") == "詳しくは公式サイトをご覧ください"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
