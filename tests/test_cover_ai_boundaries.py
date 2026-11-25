"""The Gemini and book-identity validations are owned by cover_ai."""
from src import cover_ai, metadata_gen


def test_metadata_gen_public_ai_api_is_one_implementation():
    for name in (
        "generate_gemini_art_prompt", "generate_gemini_book_introduction",
        "generate_gemini_cover_information", "review_cover_information",
        "finalize_cover_prompt", "_validate_plot_source",
    ):
        assert getattr(metadata_gen, name) is getattr(cover_ai, name)
    assert cover_ai.finalize_cover_prompt.__module__ == "src.cover_ai"
