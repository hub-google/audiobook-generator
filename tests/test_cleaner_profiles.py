import unittest

from src.cleaner import chunk_text, clean_text_content


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

    def test_fanren_opening_labels_are_removed_without_global_word_deletion(self):
        text = (
            "\n請記住本站域名:\n黃金屋\n凡人修仙傳\n\u00a0第一章 山邊小村\n"
            "二愣子睜大著雙眼。\n遠處真的有一座黃金屋。\n"
            "他說起《凡人修仙傳》這本書。"
        )
        cleaned = clean_text_content(text, "第一章 山邊小村", "凡人修仙傳")
        self.assertTrue(cleaned.startswith("二愣子睜大著雙眼。"))
        self.assertIn("一座黃金屋", cleaned)
        self.assertIn("《凡人修仙傳》", cleaned)

    def test_dazhuzai_pure_repeated_chapter_number_is_removed(self):
        title = "第一千五百五十一章 邪神隕落(大結局)"
        text = f"大主宰\n{title}\n第一千五百五十一章\n空間在無限的被拉近。"
        cleaned = clean_text_content(text, title, "大主宰")
        self.assertEqual(cleaned, "空間在無限的被拉近。")

    def test_doupo_corrupted_near_duplicate_title_is_removed(self):
        title = "第一千六百二十三章 結束，也是開始。（大結局）"
        text = (
            f"鬥破蒼穹\n{title}\n"
            "第一千六百二十蘭章結束，仇是開始（大結局）\n"
            "一場曠世之戰落幕，\n然而卻是留下了一個滿目瘡痍的中州。"
        )
        cleaned = clean_text_content(text, title, "鬥破蒼穹")
        self.assertTrue(cleaned.startswith("一場曠世之戰落幕，"))
        self.assertNotIn("大結局", cleaned)

    def test_chapter_reference_later_in_prose_is_preserved(self):
        title = "第一千五百五十一章 邪神隕落"
        text = f"{title}\n正文開始。\n他讀到第一千五百五十一章才停下。"
        cleaned = clean_text_content(text, title, "大主宰")
        self.assertIn("他讀到第一千五百五十一章才停下。", cleaned)

    def test_long_clause_prefers_pause_after_chuanlai(self):
        chunked = chunk_text("從他身上不時傳來輕重不一的陣陣打呼聲。", max_length=18)
        self.assertEqual(chunked, "從他身上不時傳來\n輕重不一的陣陣打呼聲。")


if __name__ == "__main__":
    unittest.main()
