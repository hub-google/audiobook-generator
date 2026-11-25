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

    def test_kaggle_variable_batch_validation(self):
        import json, hashlib
        from pathlib import Path
        from PIL import Image
        from src.image.kaggle_chapter_images.validate_outputs import validate_output
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifests").mkdir()
            (root / "chapter_0009").mkdir()
            rows = []
            for scene in (1, 2):
                path = root / f"chapter_0009/scene_{scene:02d}.webp"
                Image.new("RGB", (1280, 720)).save(path, "WEBP")
                rows.append({"chapter": 9, "scene": scene, "relative_path": "chapter_images/v1/images/" + path.relative_to(root).as_posix(), "status": "needs_review", "source_sha256": "source", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            chapter = {"chapter": 9, "status": "needs_review", "source_sha256": "source", "scene_paths": [r["relative_path"] for r in rows]}
            (root / "run_report.json").write_text(json.dumps({"status": "generated", "chapters": 1, "images": 2}))
            (root / "manifests/chapters.jsonl").write_text(json.dumps(chapter))
            (root / "manifests/images.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
            self.assertEqual(validate_output(root)["images"], 2)
            (root / "chapter_0009/scene_02.webp").unlink()
            with self.assertRaises(ValueError):
                validate_output(root)

    def test_workflow_shell_blocks_parse(self):
        import re, shutil, subprocess, yaml
        from pathlib import Path
        if not shutil.which("bash"):
            self.skipTest("bash unavailable")
        for path in (Path(__file__).resolve().parents[1] / ".github/workflows").glob("*.yml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in data.get("jobs", {}).values():
                for step in job.get("steps", []):
                    if "run" not in step or step.get("shell", "bash").split()[0] not in ("bash", "sh"):
                        continue
                    script = re.sub(r"\$\{\{.*?\}\}", "placeholder", step["run"], flags=re.S)
                    result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, f"{path.name}: {step.get('name', step['run'][:60])}: {result.stderr}")

    def test_degraded_manifest_preserves_other_chapters(self):
        from pathlib import Path
        from src.tts_quality import degraded_chapters, update_fallback_manifest
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "silent-fallback-manifest.json"
            update_fallback_manifest(tmp, [{"chapter": 1, "silent_fallback_used": True}, {"chapter": 2, "silent_fallback_used": True}])
            self.assertEqual(degraded_chapters(path), {1, 2})
            update_fallback_manifest(tmp, recovered_chapter=1)
            self.assertEqual(degraded_chapters(path), {2})

    def test_worker_validation_failure_is_not_reported_success(self):
        from src.worker_pipeline import run_resumable_chapter
        checkpoint = Mock()
        checkpoint.is_completed.return_value = False
        checkpoint.mark_completed.side_effect = ValueError("validation failed")
        with patch("src.worker_pipeline.stage_crawl"), patch("src.worker_pipeline.time.sleep"):
            with self.assertRaisesRegex(ValueError, "validation failed"):
                run_resumable_chapter({}, checkpoint, "https://example.com/1", 1, 0)
        checkpoint.mark_failed.assert_called()

    def test_upload_range_uses_server_acknowledgement(self):
        import importlib
        from unittest.mock import Mock
        pipeline = importlib.import_module("合併上傳.cloud_pipeline")
        partial = Mock(status_code=308, headers={"Range": "bytes=0-2"})
        complete = Mock(status_code=201)
        complete.json.return_value = {"id": "video-test"}
        with patch.object(pipeline, "read_range", side_effect=lambda source, start, end: b"x" * (end - start + 1)) as read, patch.object(pipeline, "put_chunk", side_effect=[partial, complete]):
            self.assertEqual(pipeline.upload_range("session", "source", 6, 6, 0, Mock()), (6, "video-test"))
        self.assertEqual(read.call_args_list[1].args, ("source", 3, 5))
