import pytest

from src.sources.base import SourceParseError
from src.sources.twkan import TwkanSource
from src.sources.sixk import SixKSource
from src.sources.xszj import XszjSource
from src.sources.metanovel import MetaNovelSource
from src.sources.moon17 import Moon17Source
from src.sources.book8 import Book8Source


def test_twkan():
    src = TwkanSource()
    url = src.catalog_url("https://www.twkan.cc/book/abc.html")
    result = src.parse_catalog('<h1>作品章節列表</h1><div id="allchapter"><a href="/chapter/abc/def.html">第一章</a></div>', url)
    assert result["chapter_titles"] == ["第一章"]
    body = "測試正文" * 100
    assert src.parse_chapter(f'<h1 class="pt10">第一章</h1><div id="content" class="readcontent"><p>{body}</p></div>', result["chapters"][0]) == ("第一章", body)
    with pytest.raises(SourceParseError, match="正文過短"):
        src.parse_chapter('<h1 class="pt10">第一章</h1><div id="content" class="readcontent"><p>預告</p></div>', result["chapters"][0])


def test_sixk():
    src = SixKSource()
    url = src.catalog_url("https://www.6ksu.app/novel/1.html")
    result = src.parse_catalog('<h1>作品</h1><div class="chapter-grid"><a href="/novel/1/chapter/2.html">第一章</a></div>', url)
    assert result["total_chapters"] == 1
    assert src.parse_chapter('<h1 class="reader-title">第一章</h1><article class="chapter">正文</article>', result["chapters"][0]) == ("第一章", "正文")


def test_xszj_catalog_pages():
    src = XszjSource()
    url = src.catalog_url("https://xszj.tw/book/1")
    pages = {
        url: '<h1 id="chap-list-title">作品章節目錄</h1><div class="chapter-grid"><a href="/book/1/chapter-10">第1章</a></div><div class="chapterlist-nav"><a href="/book/1/catalog/2">下一頁</a></div>',
        url[:-1]+"2": '<h1 id="chap-list-title">作品章節目錄</h1><div class="chapter-grid"><a href="/book/1/chapter-20">第2章</a></div>',
    }
    result = src.load_catalog(url, pages.__getitem__)
    assert result["chapter_titles"] == ["第1章", "第2章"]
    with pytest.raises(SourceParseError):
        src.parse_catalog(pages[url], url)


def test_meta_chapter_pages():
    src = MetaNovelSource()
    url = "https://m.metanovel.org/book/1/chapter/2"
    pages = {
        url: '<h1 class="title">第1章 （1/2）</h1><div id="content" class="content">第一段</div><div class="section-opt"><a href="?page=2">下一頁</a></div>',
        url+"?page=2": '<h1 class="title">第1章 （2/2）</h1><div id="content" class="content">第二段</div>',
    }
    assert src.load_chapter(url, pages.__getitem__) == ("第1章", "第一段\n第二段")
    with pytest.raises(SourceParseError):
        src.parse_chapter(pages[url], url)


def test_moon_rejects_partial_and_corrupt_chapter():
    src = Moon17Source()
    url = "https://www.17moon.com/book/abc/def.html"
    with pytest.raises(SourceParseError):
        src.parse_chapter('<h1 class="reader-header__title">第1章</h1><article id="article">正文</article><nav class="reader-header__nav"><a href="def_2.html">下一頁</a></nav>', url)


def test_book8_rejects_unrelated_domain():
    src = Book8Source()
    with pytest.raises(SourceParseError):
        src.parse_catalog('<div id="chapters"><a href="https://example.org/read/1/?2">第一章</a></div>', "https://8book.com/novelbooks/1/")


def test_moon_complete_pages_and_corrupt_rejection():
    src = Moon17Source()
    url = "https://www.17moon.com/book/abc/def.html"
    pages = {
        url: '<h1 class="reader-header__title">《作品》第1章(1)</h1><article id="article">第一段</article><nav class="reader-header__nav"><a href="def_2.html">下一頁</a></nav>',
        url[:-5]+"_2.html": '<h1 class="reader-header__title">《作品》第1章(2)</h1><article id="article">第二段</article>',
    }
    assert src.load_chapter(url, pages.__getitem__) == ("第1章", "第一段\n第二段")
    pages[url[:-5]+"_2.html"] = '<h1 class="reader-header__title">《作品》第1章(2)</h1><article id="article"><p>。甲</p><p>。乙</p><p>。丙</p></article>'
    with pytest.raises(SourceParseError, match="文字順序異常"):
        src.load_chapter(url, pages.__getitem__)


def test_promoted_sources_are_registered():
    from src.sources import resolve_source
    for cls, url in [(SixKSource, "https://www.6ksu.app/novel/1.html"), (XszjSource, "https://xszj.tw/book/1"), (MetaNovelSource, "https://m.metanovel.org/book/1")]:
        assert isinstance(resolve_source(url), cls)
