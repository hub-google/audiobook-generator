from unittest.mock import Mock, patch

from src.metadata_gen import fetch_book_summary_details
from src.sources import SourceAccessError


BOOK_TITLE = "凛冬领主：从每日情报开始"
CATALOG_URL = "https://www.69shuba.com/book/90427/"
METADATA_URL = "https://www.69shuba.com/book/90427.htm"


def test_summary_retries_catalog_when_metadata_page_is_restricted():
    catalog_html = f"""
        <html><head>
          <meta property="og:title" content="{BOOK_TITLE}">
          <meta property="og:novel:author" content="作者">
          <meta property="og:novel:category" content="奇幻">
          <meta property="og:description" content="這是一段足夠長的小說簡介，描述主角在凜冬世界中借助每日情報發展領地，逐步面對挑戰並成長的完整故事。">
        </head></html>
    """.encode("gb18030")
    response = Mock(content=catalog_html)

    with patch(
        "src.sources.http_client.fetch_page",
        side_effect=[SourceAccessError("HTTP 403"), response],
    ) as fetch:
        summary, source = fetch_book_summary_details(BOOK_TITLE, CATALOG_URL)

    assert source == CATALOG_URL
    assert BOOK_TITLE in summary
    assert [call.args[0] for call in fetch.call_args_list] == [METADATA_URL, CATALOG_URL]


def test_summary_uses_web_fallback_when_both_source_pages_are_restricted():
    wiki = Mock(status_code=200)
    wiki.json.return_value = {
        "extract": "這是維基百科備援的足夠長簡介，其中含有小說的世界觀、主角成長、領地經營與主要衝突，可作為封面研究的可靠摘要資料。"
    }

    with patch(
        "src.sources.http_client.fetch_page",
        side_effect=SourceAccessError("HTTP 403"),
    ) as fetch, patch("src.metadata_gen.requests.get", return_value=wiki):
        summary, source = fetch_book_summary_details(BOOK_TITLE, CATALOG_URL)

    assert source == "zh.wikipedia.org"
    assert "領地經營" in summary
    assert fetch.call_count == 2
