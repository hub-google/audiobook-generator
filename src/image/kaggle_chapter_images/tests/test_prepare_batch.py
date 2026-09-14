import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare_batch import build_batch, chapter_number, effective_chars, normalize


class PrepareBatchTests(unittest.TestCase):
    def test_numeric_order_and_exact_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for number in (10, 2, 1):
                (root / f"第{number}章 測試_raw.txt").write_text(
                    f"第{number}章 測試\n請記住本站域名:\n黃金屋\n內容{number}", encoding="utf-8"
                )
            output = root / "out" / "chapters.jsonl"
            self.assertEqual(build_batch(root, output, start=2, count=2), 2)
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["chapter"] for row in rows], [2, 10])
            self.assertNotIn("黃金屋", rows[0]["text"])
            self.assertIn("source_sha256", rows[0])
            self.assertIn("normalized_sha256", rows[0])
            self.assertTrue(rows[0]["chunks"])

    def test_rejects_unrecognized_filename(self):
        with self.assertRaises(ValueError):
            chapter_number(Path("前言.txt"))

    def test_short_chapter_is_skipped_before_ai(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "第1章 短章.txt").write_text("第1章 短章\n只有一句。", encoding="utf-8")
            output = root / "chapters.jsonl"
            build_batch(root, output, 1, 1, min_chars=20)
            row = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(row["status"], "skipped_short")
            self.assertEqual(row["skip_reason"], "effective_chars<20")

    def test_normalize_removes_known_ad_but_keeps_story(self):
        text = "第3章 測試\n故事正文。\n========\n公眾期間呢，每天兩更\n第二段正文。"
        cleaned = normalize(text)
        self.assertEqual(cleaned, "故事正文。\n第二段正文。")
        self.assertEqual(effective_chars(cleaned), 11)


if __name__ == "__main__":
    unittest.main()
