import unittest

from src.metadata_gen import generate_video_description, generate_video_title


class YoutubeMetadataTextTests(unittest.TestCase):
    def test_completed_part_title_has_prefix_and_no_zero_padding(self):
        self.assertEqual(
            generate_video_title("修真聊天群", 1, 90, part_num=1),
            "[已完結]《修真聊天群》第 1~90 章【第 1 部】",
        )

    def test_description_contains_only_requested_details(self):
        description = generate_video_description(
            "修真聊天群", 1043, 1116, pure_plot="不應出現的簡介", part_num=15
        )
        self.assertEqual(
            description,
            "歡迎訂閱、點讚、開啟小鈴鐺並分享給同好朋友！\n\n"
            "📖 小說名稱：《修真聊天群》\n"
            "📌 包含章節：第 1043 章 至 第 1116 章 【第 15 部】\n"
            "🎧 播放長度：完整連續播放無中斷 (約 10~11 小時)",
        )
        self.assertNotIn("故事整體大綱簡介", description)


if __name__ == "__main__":
    unittest.main()
