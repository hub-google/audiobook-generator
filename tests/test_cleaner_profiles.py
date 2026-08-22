import unittest

from src.cleaner import clean_text_content


class CleanerProfileTests(unittest.TestCase):
    def test_custom_literal_text_is_applied(self):
        text = "正文。頂點小說ww.2３w.om下一句。"
        cleaned = clean_text_content(text, "", "", ["頂點小說ww.2３w.om"])
        self.assertEqual(cleaned, "正文。下一句。")

    def test_regex_metacharacters_are_removed_as_literal_text(self):
        unwanted = "(新書開始了，[請支持]? .* 一起加油！)"
        text = f"正文前。{unwanted}正文後。"
        self.assertEqual(clean_text_content(text, "", "", [unwanted]), "正文前。正文後。")

    def test_regex_syntax_has_no_special_meaning(self):
        text = "正文。廣告內容。下一句。"
        self.assertEqual(clean_text_content(text, "", "", [".*"]), text)

    def test_multiline_literal_normalizes_windows_line_endings(self):
        text = "正文前。\n第一行（公告）\n第二行？\n正文後。"
        unwanted = "第一行（公告）\r\n第二行？"
        self.assertEqual(clean_text_content(text, "", "", [unwanted]), "正文前。\n正文後。")

    def test_preview_keyword_is_removed_before_line_cleanup(self):
        text = "輪回盤！\n是這個人煉制的？\n頂點小說ww.2３w.om\n下一句。"
        cleaned = clean_text_content(text, "", "完美世界", ["頂點小說ww.2３w.om"])
        self.assertEqual(cleaned, "輪回盤！\n是這個人煉制的？\n下一句。")
        self.assertNotIn("頂點小說", cleaned)

    def test_book_rules_do_not_apply_when_not_supplied(self):
        text = "正文。頂點小說ww.2３w.om下一句。"
        self.assertIn("頂點小說", clean_text_content(text, "", "", []))

    def test_repeated_literal_is_removed_everywhere(self):
        self.assertEqual(clean_text_content("廣告正文廣告", "", "", ["廣告"]), "正文")


if __name__ == "__main__":
    unittest.main()
