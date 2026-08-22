import unittest

from src.cleaner import clean_text_content


class CleanerProfileTests(unittest.TestCase):
    def test_custom_regex_is_applied(self):
        text = "正文。頂點小說ww.2３w.om下一句。"
        cleaned = clean_text_content(text, "", "", [r"頂點小說ww\.2３w\.om"])
        self.assertEqual(cleaned, "正文。下一句。")

    def test_book_rules_do_not_apply_when_not_supplied(self):
        text = "正文。頂點小說ww.2３w.om下一句。"
        self.assertIn("頂點小說", clean_text_content(text, "", "", []))


if __name__ == "__main__":
    unittest.main()
