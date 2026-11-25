import asyncio
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.cloud_queue import QueueConflict, add_tasks, empty_queue, new_task
from src.queue_dispatcher import Dispatcher
from src.tts_ms import segment_cache_key


class ReliabilityTests(unittest.TestCase):
    def test_reservation_rejects_stale_queue_before_dispatch(self):
        before = add_tasks(empty_queue(), [new_task("https://example.com/1", "A")])
        after = add_tasks(before, [new_task("https://example.com/2", "B")])
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (after, "new-sha")
        dispatcher.request = Mock()
        with self.assertRaises(QueueConflict):
            dispatcher.dispatch_next(before)
        dispatcher.store.save.assert_not_called()
        dispatcher.request.assert_not_called()

    def test_cache_distinguishes_paragraph_pause(self):
        self.assertNotEqual(
            segment_cache_key("測試。", "zh-CN-YunjianNeural", "+25%", paragraph_end=False),
            segment_cache_key("測試。", "zh-CN-YunjianNeural", "+25%", paragraph_end=True),
        )

    def test_repeated_segment_is_generated_once(self):
        from pydub import AudioSegment
        from src.tts_ms import _process_chapter_async
        calls = []
        async def fake_generate(semaphore, text, mp3, wav, *args):
            calls.append(text)
            AudioSegment.silent(duration=1000).export(wav, format="wav")
            return True
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.tts_ms._generate_one_segment", side_effect=fake_generate), patch("src.tts_ms._trim_and_add_pause"):
                files, lines = asyncio.run(_process_chapter_async(["A。", "A。", "B。"], "book", 1, tmp, "zh-CN-YunjianNeural", "+25%", "ffmpeg", 2, 1))
            self.assertEqual(len(files), 3)
            self.assertEqual(files[0], files[1])
            self.assertEqual(calls.count("A。"), 1)

    def test_silent_fallback_is_not_cached(self):
        from pydub import AudioSegment
        from src.tts_ms import _process_chapter_async
        async def fake_generate(semaphore, text, mp3, wav, label, voice, rate, ffmpeg, retries, normalized, split, fallback, duration, records, chapter, segment):
            records.append({"silent_fallback_used": True})
            AudioSegment.silent(duration=1500).export(wav, format="wav")
            return True
        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.tts_ms._generate_one_segment", side_effect=fake_generate):
                files, lines = asyncio.run(_process_chapter_async(["A。"], "book", 1, tmp, "zh-CN-YunjianNeural", "+25%", "ffmpeg", 2, 1))
            self.assertEqual(files, [])
            self.assertEqual(os.listdir(os.path.join(tmp, "SegmentCache")), [])

    def test_rerun_waits_for_new_attempt_not_stale_completed_status(self):
        from src.scrape_rerun_controller import run_controller
        client = Mock()
        failed = {"status": "completed", "conclusion": "failure", "name": "【TXT抓取】book", "run_attempt": 1}
        success = {"status": "completed", "conclusion": "success", "name": "【TXT抓取】book", "run_attempt": 2}
        client.get_run.side_effect = [failed, failed, success]
        client.get_jobs.side_effect = [[{"id": 1, "name": "🕷️ Scrape — Worker 1", "conclusion": "failure"}], []]
        with patch("src.scrape_rerun_controller.time.sleep"):
            self.assertEqual(run_controller(client, "123"), "source_run_not_failed")
        client.rerun_failed_jobs.assert_called_once_with("123")

    def test_reservation_uses_sha_of_caller_snapshot(self):
        from src.cloud_queue import update_task
        queue = add_tasks(empty_queue(), [new_task("https://example.com/1", "A")])
        task_id = queue["queue"][0]["task_id"]
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.save.side_effect = QueueConflict("concurrent edit")
        dispatcher.runs = Mock(return_value=[])
        dispatcher.cover_runs = Mock(return_value=[])
        dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
        dispatcher.request = Mock()
        with self.assertRaises(QueueConflict):
            dispatcher.dispatch_next(queue, reservation_sha="old-sha")
        self.assertEqual(dispatcher.store.save.call_args.kwargs["sha"], "old-sha")
        dispatcher.request.assert_not_called()

    def test_hf_stale_dispatch_recovery_has_bounded_delay(self):
        import importlib
        from datetime import datetime, timedelta, timezone
        pipeline = importlib.import_module("合併上傳.cloud_pipeline")
        now = datetime.now(timezone.utc)
        self.assertFalse(pipeline.resume_dispatch_stale({"status": "complete"}, now))
        self.assertFalse(pipeline.resume_dispatch_stale({"status": "resume_dispatched", "resume_dispatched_at": (now - timedelta(hours=1)).isoformat()}, now))
        self.assertTrue(pipeline.resume_dispatch_stale({"status": "resume_dispatched", "resume_dispatched_at": (now - timedelta(hours=9)).isoformat()}, now))

    def test_preflight_gui_and_dispatcher_share_retry_policy(self):
        from src.cloud_queue import preflight_status
        self.assertEqual(preflight_status({"scrape": {"status": "failed", "reason": "failure", "run_attempt": 20}}), ("preparing_assets", "scrape_rerun_pending"))
        self.assertEqual(preflight_status({"scrape": {"status": "failed", "reason": "failure", "run_attempt": 21}}), ("needs_attention", "scrape_failed_after_20_reruns"))
        self.assertEqual(preflight_status({"cover": {"status": "failed", "reason": "dispatch_unconfirmed"}}), ("needs_attention", "cover_dispatch_unconfirmed"))
