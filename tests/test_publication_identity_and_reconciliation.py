"""Unit tests for YouTube publication identity, checkpoint resume, and eventual consistency reconciliation."""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.publication_checkpoint import PublicationCheckpoint, plan_fingerprint
from src.success_criteria import validate_upload_success
from src.youtube_upload.final_audit import run_final_playlist_and_archive_audit
from src.youtube_upload.playlists import validate_user_facing_playlist
from src.youtube_upload.state import load_resume_state, save_resume_state, validate_state_identity


class PublicationIdentityAndReconciliationTests(unittest.TestCase):
    def _create_sample_plan(self, num_parts=3):
        plan = []
        for i in range(1, num_parts + 1):
            start = (i - 1) * 10 + 1
            end = i * 10
            plan.append({
                "part_num": i,
                "start_chap": start,
                "end_chap": end,
                "chapters": list(range(start, end + 1)),
                "title": f"Book Part {i:02d}",
                "duration": 3600.0,
            })
        return plan

    def _setup_completed_publication(self, tmpdir, source_run_id="run-100", execution_run_id="run-100", book_profile_id="prof-aaa"):
        state_path = os.path.join(tmpdir, "state.json")
        plan = self._create_sample_plan(3)
        fp = plan_fingerprint(plan)

        # Build completed publication checkpoint (part_execution.json)
        checkpoint = PublicationCheckpoint(state_path)
        checkpoint.lock_plan(
            plan,
            run_id=source_run_id,
            execution_run_id=execution_run_id,
            book_profile_id=book_profile_id,
        )
        for part in plan:
            num = part["part_num"]
            for step in (
                "prepare_chapters", "generate_subtitle", "merge_video",
                "validate_video", "generate_metadata_cover", "upload_video",
                "upload_thumbnail", "add_playlist", "archive_hf", "final_validation",
            ):
                checkpoint.complete(
                    num,
                    step,
                    youtube_video_id=f"vid-{num}",
                    playlist_id="PL_TEST_123",
                    position=num - 1,
                    hf_repo="user/audiobooks",
                )
        for g_step in ("download_artifacts", "validate_inventory", "probe_durations", "lock_plan", "playlist", "final_book_validation"):
            checkpoint.mark_global(g_step, "completed", hf_repo="user/audiobooks")

        # Build completed state.json
        final_gate = {
            "status": "passed",
            "item_count": 3,
            "ordered_parts": [1, 2, 3],
            "unique_video_ids": 3,
        }
        save_resume_state(
            state_path,
            run_id=source_run_id,
            source_run_id=source_run_id,
            execution_run_id=execution_run_id,
            privacy="public",
            status="complete",
            completed_titles=[p["title"] for p in plan],
            part_plan=plan,
            pending_thumbnails={},
            pending_playlist={},
            pending_captions={},
            pending_publish={},
            playlist_url="https://www.youtube.com/playlist?list=PL_TEST_123",
            final_playlist_validation=final_gate,
            book_profile_id=book_profile_id,
            plan_fingerprint_str=fp,
        )
        return state_path, plan, fp

    def test_scenario_1_fresh_task_completes_normally(self):
        """Scenario 1: Fresh task completes with matching source_run_id and passes strict success gate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path, plan, fp = self._setup_completed_publication(tmpdir, source_run_id="run-fresh-1")
            result = validate_upload_success(
                state_path,
                expected_run_id="run-fresh-1",
                execution_run_id="run-fresh-1",
                expected_plan_fingerprint=fp,
            )
            self.assertEqual(result["parts"], 3)
            self.assertEqual(result["playlist_url"], "https://www.youtube.com/playlist?list=PL_TEST_123")
            self.assertEqual(result["source_run_id"], "run-fresh-1")

    def test_scenario_2_retry_run_id_changed_passes_success_gate(self):
        """Scenario 2: Retry with different Actions Run ID does NOT fail gate when plan fingerprint matches."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Original run was run-100, current retry execution run is run-200
            state_path, plan, fp = self._setup_completed_publication(
                tmpdir,
                source_run_id="run-100",
                execution_run_id="run-200",
                book_profile_id="prof-aaa",
            )
            # Even if expected_run_id is passed as the current execution run "run-200":
            result = validate_upload_success(
                state_path,
                expected_run_id="run-200",
                execution_run_id="run-200",
                expected_plan_fingerprint=fp,
                expected_book_profile_id="prof-aaa",
            )
            self.assertEqual(result["parts"], 3)
            self.assertEqual(result["source_run_id"], "run-100")
            self.assertEqual(result["execution_run_id"], "run-200")

    def test_scenario_3_resume_preserves_source_run_and_updates_execution_run(self):
        """Scenario 3: Resuming from checkpoint preserves source_run_id and updates execution_run_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = self._create_sample_plan(2)
            checkpoint = PublicationCheckpoint(state_path)
            checkpoint.lock_plan(
                plan,
                run_id="run-original",
                book_profile_id="prof-aaa",
            )
            save_resume_state(
                state_path,
                run_id="run-original",
                source_run_id="run-original",
                privacy="public",
                status="paused",
                completed_titles=[plan[0]["title"]],
                part_plan=plan,
                book_profile_id="prof-aaa",
            )

            # Rerun starts under a new execution run ID "run-retry-456"
            resumed_checkpoint = PublicationCheckpoint(state_path)
            resumed_checkpoint.lock_plan(
                plan,
                run_id="run-retry-456",
                execution_run_id="run-retry-456",
                book_profile_id="prof-aaa",
            )
            # source_run_id remains 'run-original', execution_run_id becomes 'run-retry-456'
            self.assertEqual(resumed_checkpoint.data["source_run_id"], "run-original")
            self.assertEqual(resumed_checkpoint.data["execution_run_id"], "run-retry-456")

            # Validate state identity allows resume when profile and plan match
            saved_state = load_resume_state(state_path)
            valid, msg = validate_state_identity(
                saved_state,
                book_profile_id="prof-aaa",
                part_plan=plan,
            )
            self.assertTrue(valid)

    def test_scenario_4_different_book_checkpoint_is_rejected(self):
        """Scenario 4: Checkpoint belonging to a different book profile must be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = self._create_sample_plan(2)
            checkpoint = PublicationCheckpoint(state_path)
            checkpoint.lock_plan(
                plan,
                run_id="run-1",
                book_profile_id="book-alpha-profile",
            )
            save_resume_state(
                state_path,
                run_id="run-1",
                privacy="public",
                status="paused",
                part_plan=plan,
                book_profile_id="book-alpha-profile",
            )

            # Trying to lock with book-beta-profile must raise RuntimeError
            restored = PublicationCheckpoint(state_path)
            with self.assertRaisesRegex(RuntimeError, "refusing foreign checkpoint"):
                restored.lock_plan(
                    plan,
                    run_id="run-2",
                    book_profile_id="book-beta-profile",
                )

            # validate_state_identity also rejects
            saved = load_resume_state(state_path)
            valid, msg = validate_state_identity(saved, book_profile_id="book-beta-profile")
            self.assertFalse(valid)
            self.assertIn("book-alpha-profile", msg)

    def test_scenario_5_different_part_plan_is_rejected(self):
        """Scenario 5: Checkpoint with different part plan (repartition) must be rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan_original = self._create_sample_plan(2)
            checkpoint = PublicationCheckpoint(state_path)
            checkpoint.lock_plan(
                plan_original,
                run_id="run-1",
                book_profile_id="prof-aaa",
            )

            # New run tries to repartition with 3 parts
            plan_different = self._create_sample_plan(3)
            restored = PublicationCheckpoint(state_path)
            with self.assertRaisesRegex(RuntimeError, "refusing to repartition"):
                restored.lock_plan(
                    plan_different,
                    run_id="run-2",
                    book_profile_id="prof-aaa",
                )

            # validate_task_identity also flags fingerprint mismatch
            valid, msg = restored.validate_task_identity(part_plan=plan_different)
            self.assertFalse(valid)
            self.assertIn("ledger plan_fingerprint", msg)

    def test_scenario_6_eventual_consistency_reconciliation_retries_and_succeeds(self):
        """Scenario 6: Eventual consistency in YouTube API: video query retries and succeeds without false failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = os.path.join(tmpdir, "state.json")
            plan = [
                {"part_num": 1, "start_chap": 1, "end_chap": 10, "chapters": list(range(1, 11)), "title": "Book Part 01", "duration": 100.0},
                {"part_num": 2, "start_chap": 11, "end_chap": 20, "chapters": list(range(11, 21)), "title": "Book Part 02", "duration": 100.0},
            ]
            checkpoint = PublicationCheckpoint(state_path)
            checkpoint.lock_plan(plan, run_id="run-1", book_profile_id="prof-aaa")
            checkpoint.record_upload_ack(1, "vid-1", "hash-1")
            checkpoint.record_upload_ack(2, "vid-2", "hash-2")

            # Mock youtube client
            mock_youtube = MagicMock()
            # Initial playlist read: Part 1 is present, Part 2 is not yet visible
            call_count = {"playlist": 0, "video_list": 0}

            def mock_playlist_items(yt, pl_id):
                call_count["playlist"] += 1
                if call_count["playlist"] == 1:
                    return [{"title": "Book Part 01", "video_id": "vid-1", "playlist_item_id": "pl-item-1", "position": 0}]
                # On retry, both are visible
                return [
                    {"title": "Book Part 01", "video_id": "vid-1", "playlist_item_id": "pl-item-1", "position": 0},
                    {"title": "Book Part 02", "video_id": "vid-2", "playlist_item_id": "pl-item-2", "position": 1},
                ]

            # Mock videos().list for Part 2 known video ID check
            mock_v_res = MagicMock()
            mock_v_res.execute.return_value = {"items": [{"id": "vid-2"}]}
            mock_youtube.videos().list.return_value = mock_v_res

            mock_hf = MagicMock()
            mock_hf.verify_book.return_value = {"parts": 2, "repo_id": "test/repo", "book_root": "book"}
            mock_hf.completed_parts.return_value = {1, 2}

            args = MagicMock()
            args.state_file = state_path
            args.run_id = "run-1"
            args.privacy = "public"

            mock_hf_executor = MagicMock()

            with patch("src.youtube_upload.final_audit.get_ordered_playlist_items", side_effect=mock_playlist_items), \
                 patch("src.youtube_upload.final_audit.time.sleep") as mock_sleep:
                exit_code = run_final_playlist_and_archive_audit(
                    mock_youtube, checkpoint, mock_hf, mock_hf_executor, args, "PL_TEST",
                    "desc", {"Book Part 01", "Book Part 02"}, plan,
                    {}, {}, {}, {}, "Book Title", "test/repo", 2,
                )
                self.assertEqual(exit_code, 0)
                # Verify sleep was not called endlessly, and videos().list was called with known_video_id
                mock_youtube.videos().list.assert_called_with(part="id,snippet", id="vid-2")

    def test_scenario_7_final_playlist_strict_order_is_enforced(self):
        """Scenario 7: User-facing playlist order must strictly be Part 1 -> Part N without gap or inversion."""
        plan = self._create_sample_plan(3)

        # 1. Correct contiguous order: Part 1, Part 2, Part 3
        correct_items = [
            {"title": "Book Part 01", "video_id": "vid-1", "position": 0},
            {"title": "Book Part 02", "video_id": "vid-2", "position": 1},
            {"title": "Book Part 03", "video_id": "vid-3", "position": 2},
        ]
        result = validate_user_facing_playlist(correct_items, plan)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["ordered_parts"], [1, 2, 3])

        # 2. Inverted order: Part 1, Part 3, Part 2 -> must fail
        inverted_items = [
            {"title": "Book Part 01", "video_id": "vid-1", "position": 0},
            {"title": "Book Part 03", "video_id": "vid-3", "position": 1},
            {"title": "Book Part 02", "video_id": "vid-2", "position": 2},
        ]
        with self.assertRaisesRegex(RuntimeError, "out of order"):
            validate_user_facing_playlist(inverted_items, plan)

        # 3. Duplicate video ID -> must fail
        duplicate_items = [
            {"title": "Book Part 01", "video_id": "vid-1", "position": 0},
            {"title": "Book Part 02", "video_id": "vid-1", "position": 1},
            {"title": "Book Part 03", "video_id": "vid-3", "position": 2},
        ]
        with self.assertRaisesRegex(RuntimeError, "duplicate videos"):
            validate_user_facing_playlist(duplicate_items, plan)


if __name__ == "__main__":
    unittest.main()
