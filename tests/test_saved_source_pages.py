import json
import pytest

from src.sources import SourceParseError, resolve_source
from src.sources.registry import resolve_saved_source
from src.sources.saved_pages import SavedPageBundle
from tools.download_novel_txt import download


def test_shuba_utf8_and_gb18030_are_identical():
    src = resolve_source("https://www.69shuba.com/book/1/")
    html = '<div class="txtnav"><h1>第1章 測試</h1><p>第一<span>段正文。</span></p><p>第二段正文。</p></div>'
    url = "https://www.69shuba.com/txt/1/2"
    expected = ("第1章 測試", "第一段正文。\n第二段正文。")
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        assert src.parse_chapter(html.encode(encoding), url) == expected


def test_moon_is_live_and_saved_with_same_adapter():
    url = "https://www.17moon.com/book/abc/"
    assert resolve_saved_source(url).source_id == "moon17"
    assert resolve_source(url).source_id == "moon17"
    assert type(resolve_saved_source(url)) is type(resolve_source(url))


def test_moon_saved_multi_page_import(tmp_path, monkeypatch):
    url = "https://www.17moon.com/book/abc/"
    chapter = url + "def.html"
    page2 = url + "def_2.html"
    long_text = "這是已取得授權的測試正文。" * 30
    (tmp_path / "catalog.html").write_text('<h1 class="info-title">測試作品</h1><div id="catalogGroups"><a href="def.html">第1章 開始</a></div>', encoding="utf-8")
    (tmp_path / "a.html").write_text(f'<h1 class="reader-header__title">《測試作品》第1章 開始(1)</h1><article id="article"><p>{long_text}第一段。</p></article><nav class="reader-header__nav"><a href="def_2.html">下一頁</a></nav>', encoding="utf-8")
    (tmp_path / "b.html").write_text(f'<h1 class="reader-header__title">《測試作品》第1章 開始(2)</h1><article id="article"><p>{long_text}第二段。</p></article>', encoding="utf-8")
    (tmp_path / "pages.json").write_text(json.dumps({"source_id":"moon17","pages":{url:"catalog.html",chapter:"a.html",page2:"b.html"}}), encoding="utf-8")
    def no_network(*args, **kwargs):
        raise AssertionError("offline bundle must not fetch network")
    monkeypatch.setattr("tools.download_novel_txt.fetch_chapter_text", no_network)
    src = resolve_saved_source(url)
    bundle = SavedPageBundle(tmp_path, src, url)
    title, text = src.load_chapter(chapter, bundle.fetch)
    assert title == "第1章 開始"
    assert "第一段。\n" in text and text.endswith("第二段。")
    folder = download(url, tmp_path / "out", count=1, html_dir=tmp_path)
    assert (folder / "001.txt").exists()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["transport"] == "user_saved_html"
    bundle.pages.pop(page2)
    with pytest.raises(SourceParseError, match="缺少完整匯出頁面"):
        src.load_chapter(chapter, bundle.fetch)


def test_bundle_rejects_foreign_book_and_path_escape(tmp_path):
    src = resolve_saved_source("https://www.17moon.com/book/abc/")
    url = "https://www.17moon.com/book/abc/"
    for pages in [
        {"https://www.17moon.com/book/xyz/": "catalog.html"},
        {url: "../outside.html"},
        {"https://example.com/book/abc/": "catalog.html"},
    ]:
        (tmp_path / "pages.json").write_text(json.dumps({"source_id":"moon17","pages":pages}), encoding="utf-8")
        with pytest.raises(SourceParseError):
            SavedPageBundle(tmp_path, src, url)


def test_czbooks_zero_or_one_based_catalog_and_relative_urls():
    src = resolve_source("https://czbooks.net/n/abc")
    for first in (0, 1):
        url = "https://czbooks.net/n/abc"
        html = f'<h1>測試小說</h1><ul id="chapter-list"><li><a href="abc/def?chapterNumber={first}">第一章</a></li><li><a href="abc/ghi?chapterNumber={first+1}">第二章</a></li></ul>'
        result = src.parse_catalog(html, url)
        assert result["total_chapters"] == 2
        assert result["chapters"][0] == f"https://czbooks.net/n/abc/def?chapterNumber={first}"
        with pytest.raises(SourceParseError, match="順序不連續"):
            src.parse_catalog(html.replace(f"chapterNumber={first+1}",f"chapterNumber={first+3}"),url)


def test_czbooks_keeps_inline_text():
    src = resolve_source("https://czbooks.net/n/abc")
    html = '<h1>《測試小說》第一章</h1><div class="chapter-content"><div class="content"><p>這是<span>第一段</span>正文。</p><p>第二段正文。</p></div></div>'
    assert src.parse_chapter(html,"https://czbooks.net/n/abc/def") == ("第一章","這是第一段正文。\n第二段正文。")


def test_book8_saved_same_site_page_preserves_inline_text(tmp_path):
    from src.sources.registry import resolve_saved_source
    src = resolve_saved_source("https://8book.com/novelbooks/1/")
    text = "這是授權測試正文" * 50
    html = f'<h1>小說名稱</h1><div id="chapter-title">第一章</div><div id="content"><p>{text}<span>完整句子</span>。</p><p>第二段。</p></div>'
    title, body = src.parse_chapter(html,"https://8book.com/read/1/?2")
    assert title == "第一章"
    assert body.endswith("完整句子。\n第二段。")


def test_http_does_not_follow_unapproved_redirect():
    from unittest.mock import Mock, patch
    from src.sources.http_client import fetch_page
    src = resolve_source("https://czbooks.net/n/abc")
    response = Mock(status_code=302, headers={"Location":"https://example.com/other"})
    with patch("src.sources.http_client.requests.get", return_value=response) as get, patch("src.sources.http_client.time.sleep"):
        with pytest.raises(SourceParseError):
            fetch_page("https://czbooks.net/n/abc",src)
        assert get.call_count == 1
        assert get.call_args.kwargs["allow_redirects"] is False
