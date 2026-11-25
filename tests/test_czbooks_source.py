import pytest

from src.catalog_parser import parse_catalog, generate_config_yaml
from src.sources import resolve_source, SourceAccessError, SourceParseError

URL = "https://czbooks.net/n/demo"
CATALOG = """<div class="novel-detail"><h1><span class="title">《測試作品》</span></h1>
<span class="author"><a>測試作者</a></span><div class="description">測試簡介</div>
<div class="state"><table><tr><td>分類</td><td>奇幻</td></tr></table></div></div>
<ul id="chapter-list"><li><a href="/n/demo/abc?chapterNumber=1">第2章 中段</a></li>
<li class="volume">第一卷</li><li><a href="/n/demo/xyz?chapterNumber=0">第1章 起點</a></li>
<li><a href="/n/other/abc">其他小說</a></li>
<li><a href="/n/demo/abc?chapterNumber=1">重複連結</a></li></ul>"""


def test_czbooks_catalog_and_config(tmp_path):
    result = parse_catalog(URL, html=CATALOG)
    assert result["source_id"] == "czbooks"
    assert result["book_title"] == "測試作品"
    assert result["metadata"]["author"] == "測試作者"
    assert result["metadata"]["category"] == "奇幻"
    assert result["chapter_titles"] == ["第1章 起點", "第2章 中段"]
    assert [c["source_chapter_id"] for c in result["chapter_records"]] == ["xyz", "abc"]
    assert result["chapters"][0] == URL + "/xyz?chapterNumber=0"
    config = generate_config_yaml(URL, 1, 2, str(tmp_path / "config.yaml"), parsed_result=result)
    assert config["source_id"] == "czbooks"
    assert config["chapter_records"][1]["source_chapter_id"] == "abc"


def test_czbooks_metadata_url_and_host():
    adapter = resolve_source(URL)
    assert adapter.metadata_url(URL + "/xyz?chapterNumber=0") == URL
    assert adapter.catalog_url(URL + "/") == URL
    with pytest.raises(SourceParseError):
        resolve_source("https://czbooks.net.example.org/n/demo")
    with pytest.raises(SourceParseError):
        adapter.book_id("https://czbooks.net/")


def test_czbooks_chapter_and_errors():
    adapter = resolve_source(URL)
    html = '<h1>〖測試作品〗第1章 起點</h1><div class="content">第一段。<br>第二段。<script>ad()</script><form>回報</form></div>'
    assert adapter.parse_chapter(html, URL + "/xyz") == ("第1章 起點", "第一段。\n第二段。")
    with pytest.raises(SourceParseError):
        adapter.parse_chapter("<h1>錯誤頁</h1>", URL + "/xyz")
    with pytest.raises(SourceAccessError):
        adapter.parse_catalog("<title>Just a moment...</title>", URL)


def test_worker_delay_is_source_specific():
    assert resolve_source(URL).worker_start_jitter is None
    assert resolve_source("https://www.69shuba.com/book/1/").worker_start_jitter == (2, 5)
    assert resolve_source("https://tw.hjwzw.com/Book/1").worker_start_jitter is None
