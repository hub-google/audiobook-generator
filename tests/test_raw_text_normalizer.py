from src.raw_text_normalizer import RAW_NORMALIZER_VERSION, normalize_scraped_text


def test_normalizes_fullwidth_and_removes_inline_whitespace_but_keeps_lines():
    value = "請　記 住\tＡＢＣ．１２３\r\n下一行\u200b 文字"
    assert normalize_scraped_text(value) == "請記住ABC.123\n下一行文字"
    assert RAW_NORMALIZER_VERSION == "raw-normalizer-v1"
