import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.youtube_api_uploader import (
    artifact_worker_index,
    build_part_plan_from_inventory,
    get_run_artifact_names,
    get_playlist_video_index,
    load_resume_state,
    save_resume_state,
    set_video_thumbnail,
    select_worker_artifacts,
    ThumbnailUploadPaused,
    validate_chapter_inventory,
)
from googleapiclient.errors import HttpError
from httplib2 import Response
from src.worker_pipeline import (
    recover_incomplete_chapters,
    require_complete_worker,
    validate_chapter_completeness,
)


class YouTubeUploadPlanningTests(unittest.TestCase):
    @patch("src.youtube_api_uploader.time.sleep")
    def test_playlist_index_retries_until_new_playlist_is_visible(self, sleep):
        not_found = HttpError(
            Response({"status": "404"}),
            b'{"error":{"errors":[{"reason":"playlistNotFound"}]}}',
        )
        successful = {
            "items": [{"snippet": {
                "title": "Part 1",
                "resourceId": {"videoId": "video-1"},
            }}]
        }
        request = type("Request", (), {})()
        request.execute = unittest.mock.Mock(side_effect=[not_found, successful])
        playlist_items = type("PlaylistItems", (), {
            "list": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {
            "playlistItems": lambda self: playlist_items,
        })()

        self.assertEqual(
            get_playlist_video_index(youtube, "playlist-1"),
            {"Part 1": "video-1"},
        )
        sleep.assert_called_once_with(2)

    @patch("src.youtube_api_uploader.time.sleep")
    def test_playlist_index_stops_after_bounded_retries(self, sleep):
        not_found = HttpError(
            Response({"status": "404"}),
            b'{"error":{"errors":[{"reason":"playlistNotFound"}]}}',
        )
        request = type("Request", (), {})()
        request.execute = unittest.mock.Mock(side_effect=[not_found] * 3)
        playlist_items = type("PlaylistItems", (), {
            "list": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {
            "playlistItems": lambda self: playlist_items,
        })()

        with self.assertRaises(HttpError):
            get_playlist_video_index(
                youtube, "playlist-1", attempts=3, initial_delay=1
            )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_checkpoint_preserves_pending_thumbnail_video_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = os.path.join(temp_dir, "state.json")
            save_resume_state(
                state_path, "123", "public", "paused",
                reason="thumbnailRateLimit",
                retry_at=datetime.now(timezone.utc),
                completed_titles={"Part 11"},
                part_plan=[{"part_num": 11, "title": "Part 11"}],
                pending_thumbnails={"Part 11": "video-11"},
            )
            state = load_resume_state(state_path)
        self.assertEqual(state["version"], 3)
        self.assertEqual(state["pending_thumbnails"], {"Part 11": "video-11"})

    @patch("src.youtube_api_uploader.time.sleep")
    @patch("src.youtube_api_uploader.MediaFileUpload")
    def test_thumbnail_rate_limit_becomes_resumable_pause(self, media, sleep):
        request = type("Request", (), {"execute": lambda self: (_ for _ in ()).throw(Exception("429 uploadRateLimitExceeded"))})()
        thumbnails = type("Thumbnails", (), {"set": lambda self, **kwargs: request})()
        youtube = type("YouTube", (), {"thumbnails": lambda self: thumbnails})()
        with tempfile.NamedTemporaryFile() as cover:
            with self.assertRaises(ThumbnailUploadPaused) as raised:
                set_video_thumbnail(youtube, "video-11", cover.name, attempts=2)
        self.assertEqual(raised.exception.video_id, "video-11")
        self.assertEqual(sleep.call_count, 2)

    def test_partial_worker_cannot_report_success(self):
        with self.assertRaisesRegex(RuntimeError, "1172"):
            require_complete_worker({1172}, 16)

    def test_complete_worker_is_accepted(self):
        require_complete_worker(set(), 16)

    def test_worker_validation_requires_single_chapter_mp4(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = os.path.join(temp_dir, "Workspace", "book")
            for folder in ("Audio", "Images", "Subtitles", "Video"):
                os.makedirs(os.path.join(workspace, folder), exist_ok=True)
            files = {
                "Audio/book_chapter_103.wav": b"w" * 101,
                "Images/book_chapter_103.jpg": b"j" * 101,
                "Subtitles/book_chapter_103.srt": b"s" * 11,
            }
            for relative_path, content in files.items():
                with open(os.path.join(workspace, relative_path), "wb") as output:
                    output.write(content)
            config = {
                "book_title": "book",
                "paths": {"workspace_base": "Workspace"},
            }
            with patch("src.worker_pipeline.SRC_DIR", os.path.join(temp_dir, "src")):
                complete, failed = validate_chapter_completeness(config, [103])
        self.assertEqual(complete, [])
        self.assertEqual(failed, {103})

    def test_worker_validation_accepts_all_four_chapter_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = os.path.join(temp_dir, "Workspace", "book")
            for folder in ("Audio", "Images", "Subtitles", "Video"):
                os.makedirs(os.path.join(workspace, folder), exist_ok=True)
            files = {
                "Audio/book_chapter_103.wav": b"w" * 101,
                "Images/book_chapter_103.jpg": b"j" * 101,
                "Subtitles/book_chapter_103.srt": b"s" * 11,
                "Video/book_chapter_103.mp4": b"v" * 1001,
            }
            for relative_path, content in files.items():
                with open(os.path.join(workspace, relative_path), "wb") as output:
                    output.write(content)
            config = {
                "book_title": "book",
                "paths": {"workspace_base": "Workspace"},
            }
            with patch("src.worker_pipeline.SRC_DIR", os.path.join(temp_dir, "src")):
                complete, failed = validate_chapter_completeness(config, [103])
        self.assertEqual(complete, [103])
        self.assertEqual(failed, set())

    @patch("src.worker_pipeline.stage_video_gen")
    @patch("src.worker_pipeline.stage_image_gen")
    @patch("src.worker_pipeline.stage_tts")
    @patch("src.worker_pipeline.stage_clean")
    @patch("src.worker_pipeline.stage_crawl")
    @patch("src.worker_pipeline.validate_chapter_completeness")
    def test_missing_chapter_runs_full_automatic_recovery_pipeline(
        self, validate, crawl, clean, tts, image_gen, video_gen
    ):
        validate.return_value = ([1172], set())
        remaining = recover_incomplete_chapters(
            {"book_title": "book"},
            ["/chapter-1171", "/chapter-1172"],
            [1171, 1172],
            {1172},
            worker_id=16,
        )
        self.assertEqual(remaining, set())
        crawl.assert_called_once_with(
            {"book_title": "book"}, ["/chapter-1172"], 1172, [1172]
        )
        clean.assert_called_once_with({"book_title": "book"}, target_indices=[1172])
        tts.assert_called_once_with({"book_title": "book"}, target_indices=[1172])
        image_gen.assert_called_once_with({"book_title": "book"}, target_indices=[1172])
        video_gen.assert_called_once_with(
            {"book_title": "book"}, build_parts=False, target_indices=[1172]
        )

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
