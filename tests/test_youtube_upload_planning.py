import unittest
from unittest.mock import patch

from src.youtube_api_uploader import (
    artifact_worker_index,
    build_part_plan_from_inventory,
    get_run_artifact_names,
    select_worker_artifacts,
    validate_chapter_inventory,
)


class YouTubeUploadPlanningTests(unittest.TestCase):
    def test_artifact_query_requests_every_page(self):
        completed = type("Completed", (), {
            "returncode": 0,
            "stdout": "mp4-worker-0\nmp4-worker-19\n",
            "stderr": "",
        })()
        with patch("src.youtube_api_uploader.subprocess.run", return_value=completed) as run:
            self.assertEqual(
                get_run_artifact_names("123", "owner/repo"),
                ["mp4-worker-0", "mp4-worker-19"],
            )
        command = run.call_args.args[0]
        self.assertIn("--paginate", command)
        self.assertIn("repos/owner/repo/actions/runs/123/artifacts?per_page=100", command)

    def test_selects_every_worker_and_prefers_lightweight_artifact(self):
        names = ["video-worker-0", "video-worker-1", "mp4-worker-0", "shared-config"]
        self.assertEqual(
            select_worker_artifacts(names),
            ["mp4-worker-0", "video-worker-1"],
        )

    def test_mp4_artifacts_sort_by_worker_number(self):
        names = ["mp4-worker-6", "mp4-worker-10", "mp4-worker-0", "mp4-worker-2"]
        self.assertEqual(
            sorted(names, key=artifact_worker_index),
            ["mp4-worker-0", "mp4-worker-2", "mp4-worker-6", "mp4-worker-10"],
        )

    def test_complete_plan_is_contiguous_and_starts_at_first_chapter(self):
        inventory = [
            {"artifact": "mp4-worker-1", "chap_num": number, "dur": 4.0}
            for number in range(6, 11)
        ] + [
            {"artifact": "mp4-worker-0", "chap_num": number, "dur": 4.0}
            for number in range(1, 6)
        ]
        inventory.sort(key=lambda item: item["chap_num"])
        validate_chapter_inventory(inventory, 1, 10)
        plan = build_part_plan_from_inventory(inventory, min_seconds=8, max_seconds=12)
        self.assertEqual(
            [(part["part_num"], part["start_chap"], part["end_chap"]) for part in plan],
            [(1, 1, 3), (2, 4, 6), (3, 7, 9), (4, 10, 10)],
        )

    def test_missing_chapter_blocks_upload_planning(self):
        inventory = [
            {"artifact": "mp4-worker-0", "chap_num": 1, "dur": 10.0},
            {"artifact": "mp4-worker-0", "chap_num": 3, "dur": 10.0},
        ]
        with self.assertRaisesRegex(RuntimeError, "缺少"):
            validate_chapter_inventory(inventory, 1, 3)


if __name__ == "__main__":
    unittest.main()
