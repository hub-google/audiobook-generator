import re
from unittest.mock import Mock, patch
import pytest

from src.sources.shuba69 import Shuba69Source, fetch_69shuba_full_novels
from src.catalog_parser import fetch_69shuba_full_novels as catalog_fetch_full


SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="mybox">
<ul>
    <li>
        <a class="imgbox" href="https://www.69shuba.com/book/29590.htm">
            <img src="https://cdn.cdnshu.com/images/nocover.jpg" data-src="https://cdn.cdnshu.com/files/article/image/29/29590/29590s.jpg" />
            <div class="newnav">
                <h3><a href="https://www.69shuba.com/book/29590.htm">诡秘之主</a></h3>
                <div class="labelbox">
                    <label>爱潜水的乌贼</label>
                    <label>玄幻魔法</label>
                    <label>全本</label>
                </div>
                <ol class="ellipsis_2">蒸汽与机械的浪潮中，谁能触及非凡？</ol>
                <div class="zxzj"><p><span>最近章节</span>1417.新书已发</p></div>
            </div>
        </a>
    </li>
    <!-- Duplicate entry should be ignored -->
    <li>
        <a class="imgbox" href="https://www.69shuba.com/book/29590.htm">
            <h3>诡秘之主</h3>
        </a>
    </li>
    <li>
        <a class="imgbox" href="https://www.69shuba.com/book/74678.htm">
            <img src="https://cdn.cdnshu.com/files/74678.jpg" />
            <div class="newnav">
                <h3><a href="https://www.69shuba.com/book/74678.htm">逼我重生是吧</a></h3>
                <div class="labelbox">
                    <label>幼儿园一把手</label>
                    <label>都市小说</label>
                    <label>全本</label>
                </div>
                <ol class="ellipsis_2">年少有为的程逐在网上看到一个问题...</ol>
                <div class="zxzj"><p><span>最近章节</span>第657章 《微胖狐狸》</p></div>
            </div>
        </a>
    </li>
</ul>
</div>
</body>
</html>
"""


def test_parse_full_novels_structure_and_url_normalization():
    source = Shuba69Source()
    novels = source.parse_full_novels(SAMPLE_HTML)

    # 1. Deduplication: only 2 unique novels
    assert len(novels) == 2

    # 2. Check first novel
    first = novels[0]
    assert first["rank"] == 1
    assert first["book_id"] == "29590"
    assert first["title"] == "诡秘之主"
    assert first["author"] == "爱潜水的乌贼"
    assert first["category"] == "玄幻魔法"
    assert first["status"] == "全本"
    assert first["latest_chapter"] == "1417.新书已发"
    assert "蒸汽与机械" in first["description"]
    assert first["cover"] == "https://cdn.cdnshu.com/files/article/image/29/29590/29590s.jpg"
    # CRITICAL: catalog_url must be /book/{id}/ and NEVER .htm
    assert first["catalog_url"] == "https://www.69shuba.com/book/29590/"
    assert not first["catalog_url"].endswith(".htm")

    # 3. Check second novel
    second = novels[1]
    assert second["rank"] == 2
    assert second["book_id"] == "74678"
    assert second["title"] == "逼我重生是吧"
    assert second["author"] == "幼儿园一把手"
    assert second["category"] == "都市小说"
    assert second["latest_chapter"] == "第657章 《微胖狐狸》"
    assert second["catalog_url"] == "https://www.69shuba.com/book/74678/"


def test_fetch_full_novels_via_mock():
    mock_response = Mock()
    mock_response.content = SAMPLE_HTML.encode("gb18030")

    with patch("src.sources.http_client.fetch_page", return_value=mock_response):
        novels = fetch_69shuba_full_novels()
        assert len(novels) == 2
        assert novels[0]["title"] == "诡秘之主"
        assert novels[0]["catalog_url"] == "https://www.69shuba.com/book/29590/"

    with patch("src.sources.http_client.fetch_page", return_value=mock_response):
        catalog_novels = catalog_fetch_full()
        assert len(catalog_novels) == 2
        assert catalog_novels[1]["title"] == "逼我重生是吧"
        assert catalog_novels[1]["catalog_url"] == "https://www.69shuba.com/book/74678/"


def test_url_normalization_rule():
    def normalize_url(raw_url):
        raw_url = raw_url.strip()
        if not raw_url:
            return ""
        m = re.search(r'69shuba\.com/(?:book|txt)/(\d+)', raw_url)
        if m:
            return f"https://www.69shuba.com/book/{m.group(1)}/"
        return raw_url

    assert normalize_url("https://www.69shuba.com/book/29590.htm") == "https://www.69shuba.com/book/29590/"
    assert normalize_url("https://www.69shuba.com/book/29590/") == "https://www.69shuba.com/book/29590/"
    assert normalize_url("https://www.69shuba.com/book/29590") == "https://www.69shuba.com/book/29590/"
    assert normalize_url("https://www.69shuba.com/txt/29590/10") == "https://www.69shuba.com/book/29590/"
    assert normalize_url("https://tw.hjwzw.com/Book/Chapter/1") == "https://tw.hjwzw.com/Book/Chapter/1"
