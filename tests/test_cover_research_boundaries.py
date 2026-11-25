"""Cover book discovery uses a separate source-validation service."""
from src import cover_research, metadata_gen


def test_metadata_public_plot_research_api_is_one_implementation():
    for name in (
        "_catalog_book_url", "_decode_bing_result_url",
        "fetch_book_summary_details", "fetch_book_summary_online",
        "collect_cover_research",
    ):
        assert getattr(metadata_gen, name) is getattr(cover_research, name)


def test_selected_book_url_is_resolved_by_the_correct_source():
    url = "https://69shuba.com/book/53782/"
    metadata_url = cover_research._catalog_book_url(url)
    assert "69shuba.com" in metadata_url
    assert "53782" in metadata_url
