"""Offline regression tests for the nine-source AUDIO keyword discovery path."""
from unittest.mock import patch

import pytest
import requests

from src.sources.registry import SOURCES
from tools.audio_novel_search import candidate_url, search_novels, search_source


def test_search_rejects_unrelated_or_non_book_links():
    source = SOURCES["shuba69"]
    assert candidate_url("https://69shuba.com/book/123/", source) == "https://69shuba.com/book/123/"
    assert candidate_url("https://69shuba.com/novels/full", source) is None
    assert candidate_url("https://example.com/book/123/", source) is None
    assert candidate_url("javascript:alert(1)", source) is None
    assert candidate_url("https://69shuba.com.evil.org/book/123/", source) is None


def test_search_preserves_only_valid_unique_books_and_extracts_author():
    links = [
        ("https://69shuba.com/book/123/", "詭秘之主 - 69書吧"),
        ("https://69shuba.com/book/123/", "詭秘之主"),
        ("https://example.org/book/123/", "偽造"),
        ("https://69shuba.com/novels/full", "排行"),
    ]

    class Response:
        content = b"<html></html>"

    with patch("tools.audio_novel_search.indexed_results", return_value=(links, [])), \
         patch("tools.audio_novel_search.native_results", return_value=([], [], 1)), \
         patch("tools.audio_novel_search.fetch_page", return_value=Response()), \
         patch.object(SOURCES["shuba69"], "parse_metadata",
                      return_value={"title": "詭秘之主", "author": "愛潛水的烏賊"}):
        result = search_source("詭秘之主", "shuba69")
    assert result["status"] == "ok"
    assert len(result["results"]) == 1
    book = result["results"][0]
    assert book["title"] == "詭秘之主"
    assert book["author"] == "愛潛水的烏賊"
    assert book["metadata_verified"] is True


def test_search_keeps_book_when_details_page_is_unavailable():
    links = [("https://69shuba.com/book/123/", "詭秘之主")]
    with patch("tools.audio_novel_search.indexed_results", return_value=(links, ["ConnectionError"])), \
         patch("tools.audio_novel_search.native_results", return_value=([], [], 1)), \
         patch("tools.audio_novel_search.fetch_page", side_effect=RuntimeError("403")):
        result = search_source("詭秘之主", "shuba69")
    assert len(result["results"]) == 1
    assert result["results"][0]["metadata_verified"] is False


def test_search_source_failures_are_separate_and_not_reported_as_zero_results():
    with patch("tools.audio_novel_search.search_source", side_effect=[
            {"source_id": "czbooks", "source_name": "小說狂人", "results": [
                {"catalog_url": "https://czbooks.net/n/abc", "title": "作品"}],
             "status": "ok"},
            RuntimeError("timeout")]):
        result = search_novels("作者名", ["czbooks", "shuba69"])
    assert len(result["results"]) == 1
    assert result["sources"][1]["status"] == "unavailable"


@pytest.mark.parametrize("keyword", ["", " ", "x"*81])
def test_invalid_search_keywords_are_refused(keyword):
    with pytest.raises(ValueError):
        search_novels(keyword, ["czbooks"])


def test_restricted_sources_and_duplicate_sources_are_refused():
    for source_ids in (["unknown"], ["czbooks", "czbooks"]):
        with pytest.raises(ValueError):
            search_novels("作品", source_ids)

def test_native_search_finds_real_shuba_book_in_mocked_rankings():
    from tools.audio_novel_search import native_results
    from tests.test_shuba_full_novels import SAMPLE_HTML

    class Response:
        content = SAMPLE_HTML.encode("gb18030")

    with patch("tools.audio_novel_search.requests.get",
               side_effect=requests.ConnectionError("Public snapshot unavailable")), \
         patch("tools.audio_novel_search.fetch_page", return_value=Response()):
        matches, errors, checked = native_results("詭秘之主", SOURCES["shuba69"])
    assert checked > 0
    assert matches[0]["title"] == "诡秘之主"
    assert matches[0]["author"] == "爱潜水的乌贼"
    assert matches[0]["catalog_url"] == "https://69shuba.com/book/29590/"
    assert len(matches) == 1


def test_native_search_accepts_author_keyword_from_own_site():
    from tools.audio_novel_search import native_results
    from tests.test_shuba_full_novels import SAMPLE_HTML

    class Response:
        content = SAMPLE_HTML.encode("gb18030")

    with patch("tools.audio_novel_search.requests.get",
               side_effect=requests.ConnectionError()), \
         patch("tools.audio_novel_search.fetch_page", return_value=Response()):
        matches, _, _ = native_results("愛潛水的烏賊", SOURCES["shuba69"])
    assert [n["title"] for n in matches] == ["诡秘之主"]
