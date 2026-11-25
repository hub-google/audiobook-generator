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


def test_offline_only_moon_adapter_not_live_registered():
    url = "https://www.17moon.com/book/abc/"
    assert resolve_saved_source(url).source_id == "moon17"
    with pytest.raises(SourceParseError):
        resolve_source(url)


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
