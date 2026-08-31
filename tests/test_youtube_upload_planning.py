import os
import json
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.youtube_api_uploader import (
    resolve_part_srt,
    is_valid_chinese_caption,
    artifact_worker_index,
    build_part_plan_from_inventory,
    classify_daily_limit,
    get_run_artifact_names,
    get_playlist_video_index,
    get_or_create_playlist,
    get_channel_upload_video_index,
    add_video_to_playlist,
    is_transient_upload_error,
    is_transient_youtube_api_error,
    load_resume_state,
    save_resume_state,
    recover_completed_titles_from_playlist,
    set_video_thumbnail,
    upload_video_file,
    upload_caption_file,
    verify_published_part,
    select_worker_artifacts,
    ThumbnailUploadPaused,
    UploadPaused,
    validate_chapter_inventory,
    parse_chapter_info,
    normalize_playlist_covers_to_last_part,
    validate_user_facing_playlist,
    completed_playlist_title,
    load_measured_prepared_part_plan,
    update_playlist_metadata,
    build_video_description,
    build_chapter_timeline,
)
from googleapiclient.errors import HttpError
from httplib2 import Response
from src.worker_pipeline import (
    recover_incomplete_chapters,
    require_complete_worker,
    validate_chapter_completeness,
)


class YouTubeUploadPlanningTests(unittest.TestCase):
    def test_manual_quota_probe_preserves_future_retry_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "state.json")
            original_retry = datetime.now(timezone.utc) + timedelta(hours=6)
            save_resume_state(
                state_path, "123", "public", "paused",
                reason="quotaExceeded", retry_at=original_retry,
            )

            with patch.dict(os.environ, {"MANUAL_YOUTUBE_RETRY": "true"}):
                save_resume_state(
                    state_path, "123", "public", "paused",
                    reason="quotaExceeded",
                    retry_at=original_retry + timedelta(days=1),
                )

            self.assertEqual(
                load_resume_state(state_path)["retry_at"],
                original_retry.isoformat(),
            )

    def test_manual_quota_probe_does_not_preserve_expired_retry_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = str(Path(temp_dir) / "state.json")
            expired_retry = datetime.now(timezone.utc) - timedelta(hours=1)
            new_retry = datetime.now(timezone.utc) + timedelta(hours=23)
            save_resume_state(
                state_path, "123", "public", "paused",
                reason="quotaExceeded", retry_at=expired_retry,
            )

            with patch.dict(os.environ, {"MANUAL_YOUTUBE_RETRY": "true"}):
                save_resume_state(
                    state_path, "123", "public", "paused",
                    reason="quotaExceeded", retry_at=new_retry,
                )

            self.assertEqual(
                load_resume_state(state_path)["retry_at"],
                new_retry.isoformat(),
            )

    @patch("src.metadata_gen.save_book_metadata")
    @patch("src.youtube_api_uploader.get_media_duration")
    def test_first_run_measures_prepared_parts_before_playlist(self, duration, metadata):
        duration.side_effect = [36000.5, 35999.5]
        metadata.side_effect = [{"title": "第一部"}, {"title": "第二部"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "parts-plan.json").write_text(json.dumps({"parts": [
                {"part_num": 1, "start_chap": 1, "end_chap": 10, "chapters": list(range(1, 11))},
                {"part_num": 2, "start_chap": 11, "end_chap": 20, "chapters": list(range(11, 21))},
            ]}), encoding="utf-8")
            (root / "書_Part_01_Ch0001_to_Ch0010.mp4").touch()
            (root / "書_Part_02_Ch0011_to_Ch0020.mp4").touch()
            plan = load_measured_prepared_part_plan(temp_dir, "書")
        self.assertEqual([part["duration"] for part in plan], [36000.5, 35999.5])
        self.assertEqual([part["title"] for part in plan], ["第一部", "第二部"])

    def test_chapter_timeline_uses_unrounded_cumulative_boundaries(self):
        items = [
            {"chap_num": 1043, "chapter_title": "第1043章 孟奇", "dur": 2595.372},
            {"chap_num": 1044, "chapter_title": "第1044章 山上弟子", "dur": 2555.439},
            {"chap_num": 1045, "chapter_title": "第1045章 五行聚靈符", "dur": 1800.0},
        ]
        self.assertEqual(
            build_chapter_timeline(items),
            "⏳ 影片章節時間軸：\n"
            "00:00:00 第1043章 孟奇\n"
            "00:43:15 第1044章 山上弟子\n"
            "01:25:51 第1045章 五行聚靈符",
        )

    def test_chapter_timeline_replaces_website_number_with_output_number(self):
        items = [
            {"chap_num": 1, "chapter_title": "序章 大荒", "dur": 170},
            {"chap_num": 2, "chapter_title": "第一章 朝氣蓬勃", "dur": 463},
            {"chap_num": 3, "chapter_title": "第二章 骨文", "dur": 465},
        ]
        self.assertEqual(
            build_chapter_timeline(items),
            "⏳ 影片章節時間軸：\n"
            "00:00:00 第1章 大荒\n"
            "00:02:50 第2章 朝氣蓬勃\n"
            "00:10:33 第3章 骨文",
        )

    def test_video_description_contains_playlist_and_clickable_timeline_only(self):
        items = [
            {"chap_num": 1, "chapter_title": "第1章 甲", "dur": 30.2},
            {"chap_num": 2, "chapter_title": "第2章 乙", "dur": 30.2},
            {"chap_num": 3, "chapter_title": "第3章 丙", "dur": 30.2},
        ]
        description = build_video_description("測試書", "", "PL123", items)
        self.assertEqual(
            description,
            "▶️《測試書》播放清單全集\n"
            "https://www.youtube.com/playlist?list=PL123\n\n"
            "⏳ 影片章節時間軸：\n"
            "00:00:00 第1章 甲\n"
            "00:00:30 第2章 乙\n"
            "00:01:00 第3章 丙",
        )
        self.assertNotIn("來源網站缺失章節", description)

    def test_video_description_starts_with_named_playlist_and_url(self):
        description = build_video_description(
            "吞噬星空", "歡迎訂閱、點讚！", "PL123"
        )
        self.assertEqual(
            description,
            "▶️《吞噬星空》播放清單全集\n"
            "https://www.youtube.com/playlist?list=PL123",
        )
        self.assertNotIn("歡迎訂閱", description)
        self.assertNotIn("請依順序播放", description)

    def test_video_description_omits_timeline_for_short_final_part(self):
        playlist_only = (
            "▶️《測試書》播放清單全集\n"
            "https://www.youtube.com/playlist?list=PL123"
        )
        for items in (
            [],
            [{"chap_num": 2015, "chapter_title": "第2015章 大結局", "dur": 600}],
            [
                {"chap_num": 2014, "chapter_title": "第2014章 終戰", "dur": 600},
                {"chap_num": 2015, "chapter_title": "第2015章 大結局", "dur": 600},
            ],
        ):
            with self.subTest(chapter_count=len(items)):
                self.assertEqual(
                    build_video_description("測試書", "", "PL123", items),
                    playlist_only,
                )

    def test_timeline_validator_remains_strict_when_called_directly(self):
        with self.assertRaisesRegex(ValueError, "at least three chapters"):
            build_chapter_timeline([
                {"chap_num": 2015, "chapter_title": "第2015章 大結局", "dur": 600},
            ])

    def test_video_description_requires_playlist_id(self):
        with self.assertRaisesRegex(ValueError, "playlist id"):
            build_video_description("吞噬星空", "說明", "")

    def test_completed_playlist_title_floors_measured_total_without_zero_padding(self):
        self.assertEqual(
            completed_playlist_title("修真聊天群", 327 * 3600 + 48 * 60),
            "[已完結]《修真聊天群》327小時 全集",
        )
        self.assertEqual(
            completed_playlist_title("測試書", 7 * 3600 + 3599),
            "[已完結]《測試書》7小時 全集",
        )

    def test_update_playlist_metadata_updates_existing_playlist(self):
        youtube = MagicMock()
        update_playlist_metadata(
            youtube, "PL123", "[已完結]《測試書》7小時 全集", "說明",
        )
        youtube.playlists.return_value.update.assert_called_once_with(
            part="snippet",
            body={
                "id": "PL123",
                "snippet": {
                    "title": "[已完結]《測試書》7小時 全集",
                    "description": "說明",
                    "defaultLanguage": "zh-TW",
                },
            },
        )

    @patch("src.youtube_api_uploader.get_playlist_video_index", return_value={"Part 1": "video-1"})
    def test_final_readback_requires_video_thumbnail_caption_and_playlist(self, playlist_index):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{
                "status": {"privacyStatus": "public"},
                "snippet": {"thumbnails": {"high": {"url": "https://example/cover.jpg"}}},
            }]
        }
        youtube.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"language": "zh-TW", "status": "serving"}}]
        }

        result = verify_published_part(
            youtube, "video-1", "playlist-1", "public", attempts=1
        )

        self.assertEqual(result["youtube_video_id"], "video-1")
        playlist_index.assert_called_once_with(youtube, "playlist-1")

    @patch("src.youtube_api_uploader.get_playlist_video_index", return_value={"Part 1": "video-1"})
    def test_final_readback_accepts_youtube_traditional_chinese_language_alias(self, playlist_index):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{
                "status": {"privacyStatus": "public"},
                "snippet": {"thumbnails": {"high": {"url": "https://example/cover.jpg"}}},
            }]
        }
        youtube.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"language": "zh-Hant", "status": "serving"}}]
        }

        result = verify_published_part(
            youtube, "video-1", "playlist-1", "public", attempts=1
        )

        self.assertEqual(result["youtube_video_id"], "video-1")

    @patch("src.youtube_api_uploader.get_playlist_video_index", return_value={"Part 1": "video-1"})
    def test_final_readback_rejects_missing_thumbnail(self, playlist_index):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"privacyStatus": "public"}, "snippet": {}}]
        }
        with self.assertRaisesRegex(RuntimeError, "thumbnail cannot be read back"):
            verify_published_part(
                youtube, "video-1", "playlist-1", "public", attempts=1
            )

    @patch("src.youtube_api_uploader.get_playlist_video_index", return_value={"Part 1": "video-1"})
    def test_final_readback_rejects_failed_caption_processing(self, playlist_index):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{
                "status": {"privacyStatus": "public"},
                "snippet": {"thumbnails": {"high": {"url": "https://example/cover.jpg"}}},
            }]
        }
        youtube.captions.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {
                "language": "zh-TW",
                "status": "failed",
                "failureReason": "processingFailed",
            }}]
        }
        with self.assertRaisesRegex(RuntimeError, "caption processing failed"):
            verify_published_part(
                youtube, "video-1", "playlist-1", "public", attempts=1
            )

    @patch("src.youtube_api_uploader.MediaFileUpload")
    def test_valid_caption_file_reaches_youtube_insert(self, media_upload):
        youtube = MagicMock()
        youtube.captions.return_value.list.return_value.execute.return_value = {"items": []}
        youtube.captions.return_value.insert.return_value.execute.return_value = {"id": "caption-19"}
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "part-19.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\n測試字幕\n")

            self.assertTrue(upload_caption_file(youtube, "video-19", srt_path))

        youtube.captions.return_value.insert.assert_called_once()

    @patch("src.youtube_api_uploader.MediaFileUpload")
    @patch("src.youtube_api_uploader.time.sleep")
    def test_caption_retries_visibility_after_project_rotation(self, sleep, media_upload):
        youtube = MagicMock()
        youtube.captions.return_value.list.return_value.execute.side_effect = [
            Exception("videoNotFound"),
            {"items": []},
        ]
        youtube.captions.return_value.insert.return_value.execute.return_value = {"id": "caption-20"}
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "part-20.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\ncaption\n")

            self.assertTrue(upload_caption_file(youtube, "video-20", srt_path))

        sleep.assert_called_once_with(2)
        self.assertEqual(youtube.captions.return_value.list.return_value.execute.call_count, 2)

    @patch("src.youtube_api_uploader.MediaFileUpload")
    @patch("src.youtube_api_uploader.time.sleep")
    def test_caption_still_fails_for_persistent_video_not_found(self, sleep, media_upload):
        youtube = MagicMock()
        youtube.captions.return_value.list.return_value.execute.side_effect = Exception("videoNotFound")
        youtube.captions.return_value.insert.return_value.execute.side_effect = Exception("videoNotFound")
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "missing.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\ncaption\n")

            self.assertFalse(upload_caption_file(
                youtube, "missing-video", srt_path,
                visibility_attempts=2, initial_visibility_delay=0,
            ))

    @patch("src.youtube_api_uploader.MediaFileUpload")
    def test_caption_daily_quota_requests_safe_pause(self, media_upload):
        youtube = MagicMock()
        youtube.captions.return_value.list.return_value.execute.return_value = {"items": []}
        youtube.captions.return_value.insert.return_value.execute.side_effect = Exception(
            "quotaExceeded"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "part-19.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\ncaption\n")

            with self.assertRaises(UploadPaused) as raised:
                upload_caption_file(youtube, "video-19", srt_path)

        self.assertEqual(raised.exception.reason, "quotaExceeded")
        self.assertGreater(raised.exception.retry_at, datetime.now(timezone.utc))

    def test_playlist_daily_quota_requests_safe_pause(self):
        youtube = MagicMock()
        youtube.playlists.return_value.list.return_value.execute.side_effect = Exception(
            "quotaExceeded"
        )
        with self.assertRaises(UploadPaused) as raised:
            get_or_create_playlist(youtube, "Test playlist")
        self.assertEqual(raised.exception.reason, "quotaExceeded")

    def test_channel_upload_limit_waits_24_hours_and_15_minutes(self):
        before = datetime.now(timezone.utc)
        paused = classify_daily_limit(Exception("uploadLimitExceeded"))
        after = datetime.now(timezone.utc)
        self.assertEqual(paused.reason, "uploadLimitExceeded")
        self.assertGreaterEqual(paused.retry_at, before + timedelta(hours=24, minutes=15))
        self.assertLessEqual(paused.retry_at, after + timedelta(hours=24, minutes=15))

    def test_recovers_only_exact_planned_titles_from_playlist(self):
        completed = {"Part 1"}
        recovered = recover_completed_titles_from_playlist(
            completed,
            {"Part 1", "Part 2", "unrelated video"},
            ["Part 1", "Part 2", "Part 3"],
        )
        self.assertEqual(recovered, {"Part 1", "Part 2"})
        self.assertEqual(completed, {"Part 1", "Part 2"})

    @patch("src.youtube_api_uploader.set_video_thumbnail")
    @patch("src.youtube_api_uploader.time.sleep")
    @patch("src.youtube_api_uploader.MediaFileUpload")
    @patch("src.youtube_api_uploader.os.path.getsize", return_value=1024)
    def test_video_upload_resumes_same_request_after_ssl_disconnect(
        self, getsize, media, sleep, thumbnail
    ):
        request = type("Request", (), {})()
        request.next_chunk = unittest.mock.Mock(side_effect=[
            ssl.SSLEOFError(8, "connection closed"),
            (None, {"id": "video-1"}),
        ])
        videos = type("Videos", (), {
            "insert": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {
            "videos": lambda self: videos,
        })()

        video_id = upload_video_file(
            youtube, "part.mp4", "Part 1", "description",
            network_attempts=3, initial_retry_delay=1,
        )

        self.assertEqual(video_id, "video-1")
        self.assertEqual(request.next_chunk.call_count, 2)
        request.next_chunk.assert_called_with(num_retries=3)
        sleep.assert_called_once_with(1)
        thumbnail.assert_called_once_with(youtube, "video-1", None)

    @patch("src.youtube_api_uploader.set_video_thumbnail")
    @patch("src.youtube_api_uploader.time.sleep")
    @patch("src.youtube_api_uploader.MediaFileUpload")
    @patch("src.youtube_api_uploader.os.path.getsize", return_value=1024)
    def test_video_upload_network_retries_are_bounded(
        self, getsize, media, sleep, thumbnail
    ):
        request = type("Request", (), {})()
        request.next_chunk = unittest.mock.Mock(
            side_effect=[ssl.SSLEOFError(8, "connection closed")] * 3
        )
        videos = type("Videos", (), {
            "insert": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {
            "videos": lambda self: videos,
        })()

        with self.assertRaises(ssl.SSLEOFError):
            upload_video_file(
                youtube, "part.mp4", "Part 1", "description",
                network_attempts=3, initial_retry_delay=1,
            )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        thumbnail.assert_not_called()

    def test_ssl_eof_is_a_transient_upload_error(self):
        self.assertTrue(is_transient_upload_error(ssl.SSLEOFError()))

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
                pending_playlist={"Part 11": "video-11"},
            )
            state = load_resume_state(state_path)
        self.assertEqual(state["version"], 4)
        self.assertEqual(state["pending_thumbnails"], {"Part 11": "video-11"})
        self.assertEqual(state["pending_playlist"], {"Part 11": "video-11"})

    def test_playlist_insert_failure_is_reported(self):
        request = type("Request", (), {})()
        request.execute = unittest.mock.Mock(side_effect=Exception("network connection error"))
        playlist_items = type("PlaylistItems", (), {
            "insert": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {
            "playlistItems": lambda self: playlist_items,
        })()
        self.assertFalse(add_video_to_playlist(youtube, "playlist-1", "video-1", 0))

    def test_playlist_insert_quota_exceeded_raises_upload_paused(self):
        request = type("Request", (), {})()
        request.execute = unittest.mock.Mock(side_effect=Exception("quotaExceeded"))
        playlist_items = type("PlaylistItems", (), {
            "insert": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {
            "playlistItems": lambda self: playlist_items,
        })()
        with self.assertRaises(UploadPaused) as raised:
            add_video_to_playlist(youtube, "playlist-1", "video-1", 0)
        self.assertEqual(raised.exception.reason, "quotaExceeded")

    @patch("src.youtube_api_uploader.get_playlist_video_index")
    def test_channel_upload_index_recovers_uploaded_video_ids(self, get_index):
        get_index.return_value = {"Part 19": "video-19"}
        request = type("Request", (), {
            "execute": lambda self: {"items": [{"contentDetails": {
                "relatedPlaylists": {"uploads": "uploads-1"}
            }}]},
        })()
        channels = type("Channels", (), {
            "list": lambda self, **kwargs: request,
        })()
        youtube = type("YouTube", (), {"channels": lambda self: channels})()
        self.assertEqual(
            get_channel_upload_video_index(youtube),
            {"Part 19": "video-19"},
        )
        get_index.assert_called_once_with(youtube, "uploads-1")

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

    @patch("src.youtube_api_uploader.MediaFileUpload")
    def test_thumbnail_quota_walks_all_ten_slots_before_pausing(self, media):
        calls = {"execute": 0, "rotate": 0}

        def execute():
            calls["execute"] += 1
            raise Exception("403 quotaExceeded")

        request = type("Request", (), {"execute": lambda self: execute()})()
        thumbnails = type("Thumbnails", (), {"set": lambda self, **kwargs: request})()

        class Pool:
            accounts = [{} for _ in range(10)]

            def thumbnails(self):
                return thumbnails

            def rotate_on_quota(self, error):
                calls["rotate"] += 1
                return True

        with tempfile.NamedTemporaryFile() as cover:
            with self.assertRaises(ThumbnailUploadPaused) as raised:
                set_video_thumbnail(Pool(), "video-16", cover.name)
        self.assertEqual(calls["execute"], 30)
        self.assertEqual(calls["rotate"], 30)
        self.assertEqual(raised.exception.reason, "quotaExceeded")

    @patch("src.youtube_api_uploader.set_video_thumbnail")
    @patch("src.youtube_api_uploader.MediaFileUpload")
    def test_video_insert_quota_marks_only_upload_quota(self, media, thumbnail):
        calls = []
        failed = type("Request", (), {
            "next_chunk": lambda self, **kwargs: (_ for _ in ()).throw(Exception("quotaExceeded"))
        })()
        succeeded = type("Request", (), {
            "next_chunk": lambda self, **kwargs: (None, {"id": "video-1"})
        })()

        class Pool:
            def __init__(self):
                self.request = failed

            def videos(self):
                request = self.request
                return type("Videos", (), {"insert": lambda self, **kwargs: request})()

            def rotate_on_quota(self, error, *, upload=False):
                calls.append(upload)
                self.request = succeeded
                return True

        with tempfile.NamedTemporaryFile() as video:
            self.assertEqual(
                upload_video_file(Pool(), video.name, "Part 1", "description"),
                "video-1",
            )
        self.assertEqual(calls, [True])

    @patch("src.youtube_api_uploader.time.sleep")
    def test_playlist_metadata_retries_youtube_409_service_unavailable(self, sleep):
        unavailable = HttpError(
            Response({"status": "409"}),
            b'{"error":{"errors":[{"reason":"SERVICE_UNAVAILABLE"}]}}',
        )
        youtube = MagicMock()
        execute = youtube.playlists.return_value.update.return_value.execute
        execute.side_effect = [unavailable, None]

        self.assertTrue(update_playlist_metadata(
            youtube, "playlist-1", "title", "description",
            network_attempts=3, initial_retry_delay=1,
        ))

        self.assertEqual(execute.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertTrue(is_transient_youtube_api_error(unavailable))

    @patch("src.youtube_api_uploader.time.sleep")
    def test_playlist_metadata_does_not_retry_ordinary_409(self, sleep):
        conflict = HttpError(
            Response({"status": "409"}),
            b'{"error":{"errors":[{"reason":"conflict"}]}}',
        )
        youtube = MagicMock()
        youtube.playlists.return_value.update.return_value.execute.side_effect = conflict

        with self.assertRaises(RuntimeError):
            update_playlist_metadata(youtube, "playlist-1", "title", "description")

        sleep.assert_not_called()

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
        with self.assertRaisesRegex(RuntimeError, "unresolved_missing"):
            validate_chapter_inventory(inventory, 1, 3)

    def test_confirmed_origin_missing_chapter_is_skipped_in_part_plan(self):
        inventory = [
            {"artifact": "mp4-worker-0", "chap_num": 1, "dur": 10.0},
            {"artifact": "mp4-worker-0", "chap_num": 3, "dur": 10.0},
        ]
        result = validate_chapter_inventory(inventory, 1, 3, confirmed_missing={2})
        plan = build_part_plan_from_inventory(
            inventory, min_seconds=8, max_seconds=25, confirmed_missing={2}
        )
        self.assertEqual(result["confirmed_missing"], [2])
        self.assertEqual(plan[0]["chapters"], [1, 3])
        self.assertEqual(plan[0]["source_missing_chapters"], [2])

    def test_parse_chapter_info_part_formats(self):
        self.assertEqual(
            parse_chapter_info("吞噬星空_Part_01_Ch0001_to_Ch0003.mp4"),
            (1, 3),
        )
        self.assertEqual(
            parse_chapter_info("吞噬星空_Part_02_Ch0004_to_Ch0085.mp4"),
            (4, 85),
        )
        self.assertEqual(
            parse_chapter_info("吞噬星空_Part_01_chapter_0001_to_0085.mp4"),
            (1, 85),
        )
        self.assertEqual(
            parse_chapter_info("吞噬星空_Part_01_chapter_1_to_chapter_85.mp4"),
            (1, 85),
        )

    def test_parse_chapter_info_single_and_worker_formats(self):
        self.assertEqual(parse_chapter_info("吞噬星空_chapter_1.mp4"), (1, 1))
        self.assertEqual(parse_chapter_info("吞噬星空_chapter_0120.mp4"), (120, 120))
        self.assertEqual(parse_chapter_info("吞噬星空_Ch1.mp4"), (1, 1))
        self.assertEqual(parse_chapter_info("video-worker-0"), (1, 120))
        self.assertEqual(parse_chapter_info("video-worker-1"), (121, 240))
        self.assertEqual(parse_chapter_info("unknown_filename.mp4"), (999999, 999999))


    def test_input_dir_marks_all_global_publication_steps(self):
        from src.publication_checkpoint import GLOBAL_STEPS, PublicationCheckpoint
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            checkpoint = PublicationCheckpoint(state_file)
            prepared_plan = {
                "chapter_artifacts": {"1": "mp4-worker-0", "2": "mp4-worker-0"},
                "source_missing_chapters": [],
            }
            local_plan = [{"part_num": 1, "start_chap": 1, "end_chap": 2, "chapters": [1, 2], "title": "Part 1", "duration": 100.0}]
            artifact_count = len(set(prepared_plan.get("chapter_artifacts", {}).values()))
            chapter_count = len(prepared_plan.get("chapter_artifacts", {}))
            source_missing = prepared_plan.get("source_missing_chapters", [])
            checkpoint.mark_global("download_artifacts", "completed", artifact_count=artifact_count)
            checkpoint.mark_global("probe_durations", "completed", chapter_count=chapter_count)
            checkpoint.mark_global("validate_inventory", "completed", chapter_count=chapter_count, source_missing_chapters=source_missing)
            checkpoint.lock_plan(local_plan, run_id="123", book_title="Book")
            checkpoint.mark_global("lock_plan", "completed", part_count=len(local_plan))
            checkpoint.mark_global("playlist", "completed", playlist_id="PL123")
            checkpoint.mark_global("final_book_validation", "completed", completed_parts=1)
            for step in GLOBAL_STEPS:
                self.assertEqual(
                    checkpoint.data["global_steps"].get(step, {}).get("status"),
                    "completed",
                    f"global step {step} must be completed",
                )

    def test_final_validation_accepts_hf_completed_parts(self):
        from src.publication_checkpoint import PublicationCheckpoint
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            checkpoint = PublicationCheckpoint(state_file)
            part_plan = [{"part_num": 1, "start_chap": 1, "end_chap": 1, "chapters": [1], "title": "Part 1", "duration": 100.0}]
            checkpoint.lock_plan(part_plan, run_id="123", book_title="Book")
            completed_hf_parts = {1}
            for planned in part_plan:
                part_num = int(planned["part_num"])
                record = checkpoint.data.get("parts", {}).get(str(part_num), {})
                if ((record.get("steps") or {}).get("archive_hf") or {}).get("status") != "completed":
                    if part_num in completed_hf_parts:
                        checkpoint.complete(part_num, "archive_hf", recovered_from_hf=True, hf_repo="owner/repo")
            self.assertEqual(
                checkpoint.data["parts"]["1"]["steps"]["archive_hf"]["status"],
                "completed",
            )


    def test_is_valid_chinese_caption(self):
        self.assertTrue(is_valid_chinese_caption({"language": "zh-TW"}))
        self.assertTrue(is_valid_chinese_caption({"language": "zh-Hant"}))
        self.assertTrue(is_valid_chinese_caption({"language": "zh-HK"}))
        self.assertTrue(is_valid_chinese_caption({"language": "zh"}))
        self.assertTrue(is_valid_chinese_caption({"language": "cmn"}))
        self.assertFalse(is_valid_chinese_caption({"language": "en"}))
        self.assertFalse(is_valid_chinese_caption({}))

    def test_resolve_part_srt_finds_file_by_part_and_range(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_dir = os.path.join(temp_dir, "Upload_Subtitles")
            os.makedirs(srt_dir, exist_ok=True)
            srt_path = os.path.join(srt_dir, "Book_Part_17_Ch2977_to_Ch3156.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\n測試字幕內容\n")

            found = resolve_part_srt(
                title="《測試書》有聲小說 全集 第17部 (第2977~3156章)",
                part_num=17,
                search_dirs=[srt_dir],
            )
            self.assertIsNotNone(found)
            self.assertEqual(os.path.abspath(found), os.path.abspath(srt_path))

    @patch("src.youtube_api_uploader.upload_caption_file", return_value=True)
    @patch("src.youtube_api_uploader.get_playlist_video_index", return_value={"Part 17": "video-17"})
    def test_final_readback_self_heals_missing_caption(self, playlist_index, upload_caption):
        youtube = MagicMock()
        youtube.videos.return_value.list.return_value.execute.return_value = {
            "items": [{
                "status": {"privacyStatus": "public"},
                "snippet": {"thumbnails": {"high": {"url": "https://example/cover.jpg"}}},
            }]
        }
        # First captions list call returns empty, second returns valid caption after upload
        youtube.captions.return_value.list.return_value.execute.side_effect = [
            {"items": []},
            {"items": [{"snippet": {"language": "zh-TW", "status": "serving"}}]},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "test.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\n測試字幕\n")

            result = verify_published_part(
                youtube, "video-17", "playlist-17", "public", attempts=1,
                srt_path=srt_path, part_title="Part 17", part_num=17,
            )

        self.assertEqual(result["youtube_video_id"], "video-17")
        upload_caption.assert_called_once_with(youtube, "video-17", os.path.abspath(srt_path))

    @patch("src.youtube_api_uploader.set_video_thumbnail", return_value=True)
    def test_cover_difference_replaces_every_video_with_last_part_cover(self, set_thumbnail):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "part-1.jpg")
            last = os.path.join(directory, "part-2.jpg")
            with open(first, "wb") as handle:
                handle.write(b"old-cover")
            with open(last, "wb") as handle:
                handle.write(b"last-part-cover")
            items = [
                {"position": 0, "title": "Part 1", "video_id": "video-1"},
                {"position": 1, "title": "Part 2", "video_id": "video-2"},
            ]
            parts = [
                {"part_num": 1, "cover_path": first},
                {"part_num": 2, "cover_path": last},
            ]

            result = normalize_playlist_covers_to_last_part(MagicMock(), items, parts)

            self.assertTrue(result["cover_repair_applied"])
            self.assertEqual(result["canonical_cover_source_part"], 2)
            self.assertEqual(open(first, "rb").read(), open(last, "rb").read())
            self.assertEqual(set_thumbnail.call_count, 2)
            self.assertEqual({call.args[2] for call in set_thumbnail.call_args_list}, {last})

    def test_user_facing_gate_rejects_out_of_order_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            cover = os.path.join(directory, "cover.jpg")
            with open(cover, "wb") as handle:
                handle.write(b"same-cover")
            plan = [
                {"part_num": 1, "title": "Part 1"},
                {"part_num": 2, "title": "Part 2"},
            ]
            items = [
                {"position": 0, "title": "Part 2", "video_id": "video-2"},
                {"position": 1, "title": "Part 1", "video_id": "video-1"},
            ]
            with self.assertRaisesRegex(RuntimeError, "out of order"):
                validate_user_facing_playlist(items, plan, [cover, cover])


if __name__ == "__main__":
    unittest.main()
