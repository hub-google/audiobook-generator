import json
import os
import shutil
import tempfile
import unittest
import yaml
from unittest.mock import MagicMock, patch

from src.catalog_parser import generate_matrix_from_config, validate_and_restore_config
from src.pipeline_checkpoint import PipelineCheckpoint, STAGES
from src.prepare_parts import restore_locked_plan, restore_locked_merge_result
from src.publication_checkpoint import PublicationCheckpoint, plan_fingerprint, normalized_plan


class TestStrictResume(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)

    def _create_dummy_wav(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        import wave
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00" * 4800)  # 0.1s

    def _create_dummy_srt(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\n測試字幕\n\n")

    def _create_dummy_image(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        from PIL import Image
        im = Image.new("RGB", (1280, 720), color=(10, 20, 30))
        im.save(path, format="JPEG")

    def _create_dummy_video(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"dummy_mp4_bytes")

    def _create_dummy_txt(self, path, text="第1章 測試章節\n內文第一行。"):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _create_chapter_files(self, cp, chapter=1):
        for stage in STAGES:
            out_path = cp.output_path(chapter, stage)
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            if stage in ("crawler", "cleaner"):
                self._create_dummy_txt(out_path)
            elif stage == "image":
                self._create_dummy_image(out_path)
            elif stage == "tts":
                self._create_dummy_wav(out_path)
            elif stage == "subtitle":
                self._create_dummy_srt(out_path)
            elif stage == "video":
                self._create_dummy_video(out_path)

    # ─────────────────────────────────────────────────────────────
    # 1. 完全相同 Resume 零重做
    # ─────────────────────────────────────────────────────────────
    def test_identical_resume_zero_redos(self):
        """Identical resume should result in zero stages redone."""
        ws = os.path.join(self.tmpdir, "Workspace")
        book = "TestBook"
        book_dir = os.path.join(ws, book)

        chapter_sources = {
            1: {
                "output_chapter": 1, "source_index": 1,
                "chapter_url": "https://example.com/1",
                "chapter_title": "第1章",
                "source_identity": "1|https://example.com/1",
            }
        }

        with patch("src.pipeline_checkpoint.validate_stage", side_effect=lambda stage, cand, **kw: {"sha256": f"mock_{stage}", "duration_seconds": 1.0}):
            cp1 = PipelineCheckpoint(
                book_dir, book, 0, [1],
                cleaner_fingerprint="fp1",
                chapter_sources=chapter_sources,
            )
            self._create_chapter_files(cp1, 1)

            # Mark all completed
            for stage in STAGES:
                cp1.mark_completed(1, stage)

            # Sanity check: cp1 has 0 incomplete
            self.assertEqual(cp1.incomplete_chapters(), [])

            # Resume in a new instance with identical config
            cp2 = PipelineCheckpoint(
                book_dir, book, 0, [1],
                cleaner_fingerprint="fp1",
                chapter_sources=chapter_sources,
            )
            cp2.reconcile()

            # Zero redos: all stages remain completed, 0 incomplete
            self.assertEqual(cp2.incomplete_chapters(), [])
            for stage in STAGES:
                self.assertTrue(cp2.is_completed(1, stage), f"Stage {stage} should remain completed")

    # ─────────────────────────────────────────────────────────────
    # 2. 手動 Resume 不丟設定，缺值直接報錯
    # ─────────────────────────────────────────────────────────────
    def test_manual_resume_preserves_all_settings_and_fails_on_missing(self):
        """Resume strictly preserves all book profile settings and fails immediately on missing values."""
        cfg_path = os.path.join(self.tmpdir, "config.yaml")
        valid_data = {
            "book_title": "少年仙尊",
            "book_profile_id": "profile-123",
            "profile_revision": 2,
            "cleaner": {
                "fingerprint": "cleaner-fp-xyz",
                "remove_patterns": ["廣告", "請假條"],
            },
            "manual_cover": {"enabled": False},
            "selected_indices": [1, 2, 3],
            "source_indices": [1, 2, 3],
            "chapters": ["https://ex.com/1", "https://ex.com/2", "https://ex.com/3"],
            "chapter_order": [1, 2, 3],
            "chapters_per_worker": 2,
        }
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(valid_data, f)

        out_cfg = os.path.join(self.tmpdir, "restored_config.yaml")
        out_matrix = os.path.join(self.tmpdir, "matrix.json")

        restored = validate_and_restore_config(cfg_path, out_cfg, out_matrix)
        self.assertEqual(restored["book_title"], "少年仙尊")
        self.assertEqual(restored["cleaner"]["fingerprint"], "cleaner-fp-xyz")
        self.assertEqual(restored["profile_revision"], 2)

        # Check matrix generated correctly
        with open(out_matrix, "r", encoding="utf-8") as f:
            matrix = json.load(f)
        self.assertEqual(len(matrix["include"]), 2)  # 3 chapters with cpw=2 -> 2 workers
        self.assertEqual(matrix["include"][0]["start_chap"], 1)
        self.assertEqual(matrix["include"][0]["end_chap"], 2)
        self.assertEqual(matrix["include"][1]["start_chap"], 3)
        self.assertEqual(matrix["include"][1]["end_chap"], 3)

        # Test: missing cleaner.fingerprint must fail
        invalid_data = dict(valid_data)
        invalid_data["cleaner"] = {"remove_patterns": ["foo"], "fingerprint": ""}
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(invalid_data, f)
        with self.assertRaisesRegex(RuntimeError, "cleaner.fingerprint is missing or empty"):
            validate_and_restore_config(cfg_path)

        # Test: missing chapter_order must fail
        invalid_data = dict(valid_data)
        del invalid_data["chapter_order"]
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(invalid_data, f)
        with self.assertRaisesRegex(RuntimeError, "missing chapter_order"):
            validate_and_restore_config(cfg_path)

        # Test: missing source_indices must fail
        invalid_data = dict(valid_data)
        del invalid_data["source_indices"]
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(invalid_data, f)
        with self.assertRaisesRegex(RuntimeError, "missing selected_indices|source_indices"):
            validate_and_restore_config(cfg_path)

    # ─────────────────────────────────────────────────────────────
    # 3. 單一 stage 改變只重做正確下游（cleaner 改變不影響 image）
    # ─────────────────────────────────────────────────────────────
    def test_single_stage_change_only_redoes_correct_downstream(self):
        """When cleaner changes, cleaner->tts->subtitle->video are redone; image and crawler are preserved."""
        ws = os.path.join(self.tmpdir, "Workspace")
        book = "TestBook"
        book_dir = os.path.join(ws, book)

        chapter_sources = {
            1: {
                "output_chapter": 1, "source_index": 1,
                "chapter_url": "https://example.com/1",
                "chapter_title": "第1章",
                "source_identity": "1|https://example.com/1",
            }
        }

        with patch("src.pipeline_checkpoint.validate_stage", side_effect=lambda stage, cand, **kw: {"sha256": f"mock_{stage}", "duration_seconds": 1.0}):
            cp = PipelineCheckpoint(
                book_dir, book, 0, [1],
                cleaner_fingerprint="fp_old",
                chapter_sources=chapter_sources,
            )
            self._create_chapter_files(cp, 1)

            for stage in STAGES:
                cp.mark_completed(1, stage)

            # Now resume with NEW cleaner fingerprint
            cp_changed = PipelineCheckpoint(
                book_dir, book, 0, [1],
                cleaner_fingerprint="fp_NEW",
                chapter_sources=chapter_sources,
            )
            cp_changed.reconcile()

            ch1 = cp_changed.data["chapters"]["1"]["stages"]
            # Crawler: preserved!
            self.assertEqual(ch1["crawler"]["status"], "completed")
            # Image: preserved! (Only depends on crawler!)
            self.assertEqual(ch1["image"]["status"], "completed")

            # Cleaner: settings changed -> pending!
            self.assertEqual(ch1["cleaner"]["status"], "pending")
            # TTS: depends on cleaner -> pending!
            self.assertEqual(ch1["tts"]["status"], "pending")
            # Subtitle: depends on cleaner and tts -> pending!
            self.assertEqual(ch1["subtitle"]["status"], "pending")
            # Video: depends on tts, subtitle, image -> pending!
            self.assertEqual(ch1["video"]["status"], "pending")

    # ─────────────────────────────────────────────────────────────
    # 4. 每章來源身份變動檢測
    # ─────────────────────────────────────────────────────────────
    def test_chapter_source_identity_verification(self):
        """If catalog order/URL shifts for a chapter, all stages for that chapter are invalidated."""
        ws = os.path.join(self.tmpdir, "Workspace")
        book = "TestBook"
        book_dir = os.path.join(ws, book)

        chapter_sources_1 = {
            1: {
                "output_chapter": 1, "source_index": 1,
                "chapter_url": "https://example.com/chap/100",
                "chapter_title": "第1章 原本章節",
                "source_identity": "1|https://example.com/chap/100",
            }
        }

        with patch("src.pipeline_checkpoint.validate_stage", side_effect=lambda stage, cand, **kw: {"sha256": f"mock_{stage}", "duration_seconds": 1.0}):
            cp1 = PipelineCheckpoint(
                book_dir, book, 0, [1],
                cleaner_fingerprint="fp1",
                chapter_sources=chapter_sources_1,
            )
            self._create_chapter_files(cp1, 1)

            for stage in STAGES:
                cp1.mark_completed(1, stage)

            # Catalog changed: output chapter 1 now points to a different source URL!
            chapter_sources_changed = {
                1: {
                    "output_chapter": 1, "source_index": 2,
                    "chapter_url": "https://example.com/chap/999_DIFFERENT",
                    "chapter_title": "第1章 替換章節",
                    "source_identity": "2|https://example.com/chap/999_DIFFERENT",
                }
            }

            cp2 = PipelineCheckpoint(
                book_dir, book, 0, [1],
                cleaner_fingerprint="fp1",
                chapter_sources=chapter_sources_changed,
            )
            cp2.reconcile()

            # Chapter 1 must be completely invalidated
            ch1 = cp2.data["chapters"]["1"]["stages"]
            for stage in STAGES:
                self.assertEqual(ch1[stage]["status"], "pending", f"Stage {stage} should be invalidated")

    # ─────────────────────────────────────────────────────────────
    # 5. UNKNOWN / 缺值直接報錯停止
    # ─────────────────────────────────────────────────────────────
    def test_unknown_or_missing_signature_raises_error(self):
        """Missing or UNKNOWN signatures in completed records must raise RuntimeError."""
        ws = os.path.join(self.tmpdir, "Workspace")
        book = "TestBook"
        book_dir = os.path.join(ws, book)

        with patch("src.pipeline_checkpoint.validate_stage", side_effect=lambda stage, cand, **kw: {"sha256": f"mock_{stage}", "duration_seconds": 1.0}):
            cp = PipelineCheckpoint(book_dir, book, 0, [1], cleaner_fingerprint="fp1")
            self._create_chapter_files(cp, 1)
            cp.mark_completed(1, "crawler")

            # Corrupt the record by setting input_signature to None
            cp.data["chapters"]["1"]["stages"]["crawler"]["input_signature"] = None
            cp.save()

            # Reconcile or is_completed must raise RuntimeError
            with self.assertRaisesRegex(RuntimeError, "UNKNOWN/missing signature"):
                cp.reconcile()

            with self.assertRaisesRegex(RuntimeError, "UNKNOWN/missing signature"):
                cp.is_completed(1, "crawler")

    # ─────────────────────────────────────────────────────────────
    # 6. Part Plan 必須沿用鎖定內容，不准重新排序或重做
    # ─────────────────────────────────────────────────────────────
    def test_part_plan_does_not_reorder_or_repartition(self):
        """restore_locked_plan strictly verifies locked parts-plan.json and refuses mismatch."""
        plan_dir = os.path.join(self.tmpdir, "source_prepared_plan")
        os.makedirs(plan_dir, exist_ok=True)
        out_dir = os.path.join(self.tmpdir, "out_plan")

        source_config = {
            "book_title": "小說",
            "book_profile_id": "prof-1",
            "selected_indices": [1, 2, 3, 4],
            "cleaner": {"fingerprint": "fp1"},
        }
        with open(os.path.join(plan_dir, "config.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(source_config, f)

        plan_data = {
            "source_run_id": "11111",
            "selected_indices": [1, 2, 3, 4],
            "parts": [
                {"part_num": 1, "start_chap": 1, "end_chap": 2, "chapters": [1, 2], "title": "Part 01", "duration": 100.0},
                {"part_num": 2, "start_chap": 3, "end_chap": 4, "chapters": [3, 4], "title": "Part 02", "duration": 120.0},
            ],
            "matrix": {"include": [{"part_numbers": "1,2", "merge_worker_id": 0}]},
        }
        with open(os.path.join(plan_dir, "parts-plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan_data, f)

        curr_cfg_path = os.path.join(self.tmpdir, "current_config.yaml")
        with open(curr_cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(source_config, f)

        # Successful restore
        restored = restore_locked_plan(plan_dir, curr_cfg_path, out_dir)
        self.assertEqual(len(restored["parts"]), 2)
        self.assertEqual(restored["parts"][0]["part_num"], 1)
        self.assertEqual(restored["parts"][1]["part_num"], 2)

        # Mismatch in selected_indices must raise RuntimeError
        mismatched_cfg = dict(source_config)
        mismatched_cfg["selected_indices"] = [1, 2, 3]  # changed
        with open(curr_cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(mismatched_cfg, f)

        self.assertIsNone(restore_locked_plan(plan_dir, curr_cfg_path, out_dir))

    # ─────────────────────────────────────────────────────────────
    # 7. YouTube Resume 複用已成功 Part，從失敗 Part 繼續
    # ─────────────────────────────────────────────────────────────
    def test_youtube_resumes_from_failed_part_and_skips_completed_items(self):
        """YouTube publication skips already uploaded videos and only resumes unfinished items."""
        state_file = os.path.join(self.tmpdir, "state.json")
        checkpoint = PublicationCheckpoint(state_file)
        plan = [
            {"part_num": 1, "start_chap": 1, "end_chap": 5, "chapters": [1, 2, 3, 4, 5], "duration": 3600.0, "title": "Part 01"},
            {"part_num": 2, "start_chap": 6, "end_chap": 10, "chapters": [6, 7, 8, 9, 10], "duration": 3800.0, "title": "Part 02"},
        ]
        checkpoint.lock_plan(plan, run_id="run-1", book_title="Test Book")

        # Part 1 was completely finished
        checkpoint.record_upload_ack(1, "vid-1", "sha-part1", youtube_slot=1)
        checkpoint.record_thumbnail_ack(1)
        checkpoint.record_playlist_ack(1, "pli-1", 0)

        # Part 2 had uploaded video, but thumbnail and playlist failed
        checkpoint.record_upload_ack(2, "vid-2", "sha-part2", youtube_slot=1)

        # Reload checkpoint
        restored_cp = PublicationCheckpoint(state_file)
        p1 = restored_cp.data["parts"]["1"]
        p2 = restored_cp.data["parts"]["2"]

        # Part 1: All completed
        self.assertEqual(p1["upload"]["status"], "completed")
        self.assertEqual(p1["upload"]["video_id"], "vid-1")
        self.assertEqual(p1["thumbnail"]["status"], "completed")
        self.assertEqual(p1["playlist"]["status"], "completed")
        self.assertEqual(p1["playlist"]["playlist_item_id"], "pli-1")

        # Part 2: Video uploaded, but thumbnail and playlist pending
        self.assertEqual(p2["upload"]["status"], "completed")
        self.assertEqual(p2["upload"]["video_id"], "vid-2")
        self.assertEqual(p2["thumbnail"]["status"], "pending")
        self.assertEqual(p2["playlist"]["status"], "pending")

        self.assertEqual(restored_cp.data["plan"][0]["part_num"], 1)
        self.assertEqual(restored_cp.data["plan"][1]["part_num"], 2)


if __name__ == "__main__":
    unittest.main()
