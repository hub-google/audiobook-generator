import unittest

from src.cleaner import clean_text_content


class CleanerProfileTests(unittest.TestCase):
    def test_custom_regex_is_applied(self):
        text = "正文。頂點小說ww.2３w.om下一句。"
        cleaned = clean_text_content(text, "", "", [r"頂點小說ww\.2３w\.om"])
        self.assertEqual(cleaned, "正文。下一句。")

    def test_preview_keyword_is_removed_before_line_cleanup(self):
        text = "輪回盤！\n是這個人煉制的？\n頂點小說ww.2３w.om\n下一句。"
        cleaned = clean_text_content(text, "", "完美世界", ["頂點小說ww.2３w.om"])
        self.assertEqual(cleaned, "輪回盤！\n是這個人煉制的？\n下一句。")
        self.assertNotIn("頂點小說", cleaned)

    def test_book_rules_do_not_apply_when_not_supplied(self):
        text = "正文。頂點小說ww.2３w.om下一句。"
        self.assertIn("頂點小說", clean_text_content(text, "", "", []))


if __name__ == "__main__":
    unittest.main()
