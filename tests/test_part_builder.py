"""
test_part_builder.py — 驗證 10~11 小時無縫分部 (Part) 演算法單元測試
"""

import sys
import os
import unittest

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC_DIR)

from part_builder import partition_chapters

class TestPartBuilder(unittest.TestCase):

    def test_seamless_partitioning(self):
        """測試 200 個章節（每章約 5 分鐘 / 300 秒），總共 16.6 小時，驗證是否無縫切分為 Part 1 (10.5h) 與 Part 2 (6.1h)"""
        file_list = []
        mock_items = []
        for i in range(1, 201):
            fp = f"Workspace/Test/Video/Test_chapter_{i}.mp4"
            file_list.append(fp)
            mock_items.append({"path": fp, "chap_num": i, "dur": 300.0}) # 每章 300 秒 = 5 分鐘

        # 覆蓋 get_media_duration 測試
        def mock_get_dur(path):
            m = [x for x in mock_items if x["path"] == path]
            return m[0]["dur"] if m else 300.0

        import part_builder
        original_get_dur = part_builder.get_media_duration
        part_builder.get_media_duration = mock_get_dur

        try:
            parts = partition_chapters(file_list, min_hours=10.0, max_hours=11.0)
            
            self.assertGreaterEqual(len(parts), 1)
            
            # 驗證第一部
            part1 = parts[0]
            self.assertEqual(part1["part_num"], 1)
            self.assertEqual(part1["start_chap"], 1)
            # 132 章 * 300s = 39600s = 精準 11.0 小時
            self.assertEqual(part1["end_chap"], 132)
            self.assertGreaterEqual(part1["duration"], 36000.0) # >= 10h
            self.assertLessEqual(part1["duration"], 39600.0)    # <= 11h
            
            # 驗證第二部精準銜接
            part2 = parts[1]
            self.assertEqual(part2["part_num"], 2)
            self.assertEqual(part2["start_chap"], 133) # 必為 132 + 1 = 133，絕對無縫！
            self.assertEqual(part2["end_chap"], 200)
            
            print("\n[SUCCESS] Unit test passed: Part 1 (Ch 1~132, 11.0h) -> Part 2 (Ch 133~200, 5.67h), 100% contiguous chapters!")

        finally:
            part_builder.get_media_duration = original_get_dur

if __name__ == "__main__":
    unittest.main()
