"""Regression tests for fixes exposed during run 33737288928.

Tests:
1. playlistItems.insert is called ONLY ONCE even when playlistItems.list suffers from eventual consistency lag.
2. Under locked plan, measured MP4 duration is retained in the runtime copy passed to final audit.
3. Resume state having part_plan without duration recovers duration from input_dir MP4s.
4. Any part with duration <= 0 fails immediately.
5. Duration differences do not affect plan fingerprint (locked plan remains immutable).
6. Remote evidence strictly recovers only upload_video, add_playlist, and archive_hf, never local validation steps.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.publication_checkpoint import PublicationCheckpoint, plan_fingerprint, normalized_plan
from src.youtube_upload.final_audit import run_final_playlist_and_archive_audit
from src.youtube_upload.local_pipeline import run_local_prepared_parts_mode
from src.youtube_upload.orchestrator import run_upload_pipeline
from src.youtube_upload.state import load_resume_state, save_resume_state


class Run33737288928RegressionTests(unittest.TestCase):

    def _sample_plan(self, num_parts=2, duration=3600.0):
        plan = []
        for i in range(1, num_parts + 1):
            plan.append({
                "part_num": i,
                "start_chap": (i - 1) * 10 + 1,
                "end_chap": i * 10,
                "chapters": list(range((i - 1) * 10 + 1, i * 10 + 1)),
                "title": f"Book Part {i:02d}",
                "duration": duration,
            })
        return plan

    def test_regression_1_playlist_insert_called_only_once_under_eventual_consistency(self):
        """Regression 1: playlistItems.insert succeeds, but initial playlistItems.list lags.

        Verify playlistItems.insert is called ONLY ONCE in total, and final_audit does NOT insert again.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            plan = self._sample_plan(2)
            checkpoint = PublicationCheckpoint(state_file)
            checkpoint.lock_plan(plan, run_id="run-1", book_title="Test Book")

            # Part 1 and Part 2 are uploaded.
            # Part 1 has playlist ack. Part 2 gets playlist ack.
            checkpoint.record_upload_ack(1, "vid-1", "hash-1")
            checkpoint.record_playlist_ack(1, "pli-1", 0)
            checkpoint.record_upload_ack(2, "vid-2", "hash-2")
            checkpoint.record_playlist_ack(2, "pli-2", 1)

            insert_calls = []
            list_call_count = [0]

            mock_youtube = MagicMock()

            # Mock playlistItems().insert()
            def mock_insert(**kwargs):
                insert_calls.append(kwargs)
                mock_req = MagicMock()
                mock_req.execute.return_value = {"id": f"pli-{len(insert_calls)}"}
                return mock_req

            mock_youtube.playlistItems().insert = mock_insert

            # Eventual consistency: first list call only sees Part 1. Second list call sees Part 1 and Part 2.
            def mock_list(**kwargs):
                list_call_count[0] += 1
                mock_req = MagicMock()
                if list_call_count[0] == 1:
                    items = [
                        {
                            "id": "pli-1",
                            "snippet": {
                                "title": "Book Part 01",
                                "position": 0,
                                "resourceId": {"videoId": "vid-1"},
                            },
                        }
                    ]
                else:
                    items = [
                        {
                            "id": "pli-1",
                            "snippet": {
                                "title": "Book Part 01",
                                "position": 0,
                                "resourceId": {"videoId": "vid-1"},
                            },
                        },
                        {
                            "id": "pli-2",
                            "snippet": {
                                "title": "Book Part 02",
                                "position": 1,
                                "resourceId": {"videoId": "vid-2"},
                            },
                        },
                    ]
                mock_req.execute.return_value = {"items": items, "nextPageToken": None}
                return mock_req

            mock_youtube.playlistItems().list = mock_list

            mock_v_res = MagicMock()
            mock_v_res.execute.return_value = {"items": [{"id": "vid-2"}]}
            mock_youtube.videos().list.return_value = mock_v_res

            mock_hf = MagicMock()
            mock_hf.verify_book.return_value = {"parts": 2, "repo_id": "test/repo", "book_root": "book"}
            mock_hf.completed_parts.return_value = {1, 2}
            mock_hf_executor = MagicMock()

            args = MagicMock()
            args.state_file = state_file
            args.run_id = "run-1"
            args.privacy = "public"

            with patch("src.youtube_upload.final_audit.time.sleep") as mock_sleep:
                exit_code = run_final_playlist_and_archive_audit(
                    mock_youtube, checkpoint, mock_hf, mock_hf_executor, args, "PL_TEST",
                    "desc", {"Book Part 01", "Book Part 02"}, plan,
                    {}, {}, {}, {}, "Test Book", "test/repo", 2,
                )
                self.assertEqual(exit_code, 0)
                # playlistItems.insert must NEVER have been called by final audit!
                self.assertEqual(len(insert_calls), 0)
                # event consistency retry was executed
                self.assertGreaterEqual(list_call_count[0], 2)

    def test_regression_2_locked_plan_preserves_duration_in_runtime_copy(self):
        """Regression 2: In local_pipeline, locked plan has no duration, but runtime copy overlay retains duration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            checkpoint = PublicationCheckpoint(state_file)

            # 1. Lock plan initially (e.g. from orchestrator)
            initial_plan = self._sample_plan(2, duration=5000.0)
            locked_plan = checkpoint.lock_plan(initial_plan, run_id="run-1", book_title="Test Book")
            # publication.data["plan"] normalized plan intentionally does NOT have duration
            self.assertTrue(all("duration" not in p for p in locked_plan))

            # 2. Setup mock input_dir with prepared parts
            input_dir = os.path.join(tmpdir, "prepared_parts")
            os.makedirs(input_dir, exist_ok=True)
            parts_plan_data = {
                "parts": [
                    {"part_num": 1, "start_chap": 1, "end_chap": 10},
                    {"part_num": 2, "start_chap": 11, "end_chap": 20},
                ]
            }
            with open(os.path.join(input_dir, "parts-plan.json"), "w", encoding="utf-8") as f:
                json.dump(parts_plan_data, f)

            # Create dummy MP4 and SRT files
            for p in [1, 2]:
                s = (p - 1) * 10 + 1
                e = p * 10
                fname = f"Test_Part_{p:02d}_Ch{s:04d}_to_Ch{e:04d}.mp4"
                with open(os.path.join(input_dir, fname), "wb") as f:
                    f.write(b"fake mp4")
                with open(os.path.join(input_dir, fname.replace(".mp4", ".srt")), "w", encoding="utf-8") as f:
                    f.write("1\n00:00:00,000 --> 00:00:01,000\ntest\n")

            args = MagicMock()
            args.auth_pool = False
            args.input_dir = input_dir
            args.state_file = state_file
            args.run_id = "run-1"
            args.privacy = "public"

            mock_youtube = MagicMock()
            mock_youtube.active_account = {"slot": 1}
            mock_hf = MagicMock()
            mock_hf.completed_parts.return_value = set()
            mock_hf.finalize_part.return_value = {"root": "test/path"}
            mock_hf_executor = MagicMock()

            with patch("src.youtube_upload.local_pipeline.get_media_duration", return_value=7200.0), \
                 patch("src.youtube_upload.local_pipeline.save_book_metadata", side_effect=lambda **kw: {"title": f"Book Part {kw.get('part_num', 1):02d}", "description": "desc", "cover_file": "cover.jpg", "master_cover_file": "cover.jpg"}), \
                 patch("src.youtube_upload.local_pipeline.validate_srt", return_value={"cue_count": 1}), \
                 patch("src.youtube_upload.local_pipeline.validate_video", return_value={"bytes": 100}), \
                 patch("src.youtube_upload.local_pipeline.validate_image", return_value={"sha256": "abc"}), \
                 patch("src.youtube_upload.local_pipeline.upload_video_file", side_effect=["vid-1", "vid-2"]), \
                 patch("src.youtube_upload.local_pipeline.set_video_thumbnail"), \
                 patch("src.youtube_upload.local_pipeline.add_video_to_playlist", side_effect=["pli-1", "pli-2"]):

                parts_to_upload, part_plan, total_uploaded, code = run_local_prepared_parts_mode(
                    mock_youtube, checkpoint, mock_hf, mock_hf_executor, args, "PL_TEST",
                    set(), set(), {}, locked_plan,
                    {}, {}, {}, {}, "Test Book", 1, 20, "config.yaml", "test/repo",
                    tmpdir, input_dir,
                )
                self.assertEqual(code, 0)
                # Crucial assertion: part_plan runtime copy MUST retain measured duration
                self.assertTrue(all(float(p.get("duration") or 0) == 7200.0 for p in part_plan))
                # publication checkpoint plan itself remains normalized without duration
                self.assertTrue(all("duration" not in p for p in checkpoint.data["plan"]))

    def test_regression_3_resume_state_recovers_missing_duration_from_mp4(self):
        """Regression 3: Resume state has part_plan but without duration; orchestrator recovers duration from input_dir MP4s."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            # Saved state from previous attempt with no duration
            plan_no_dur = [
                {"part_num": 1, "start_chap": 1, "end_chap": 10, "title": "Book Part 01"},
                {"part_num": 2, "start_chap": 11, "end_chap": 20, "title": "Book Part 02"},
            ]
            save_resume_state(
                state_file, run_id="run-1", privacy="public", status="running",
                completed_titles=[], part_plan=plan_no_dur,
            )

            input_dir = os.path.join(tmpdir, "prepared_parts")
            os.makedirs(input_dir, exist_ok=True)
            with open(os.path.join(input_dir, "config.yaml"), "w", encoding="utf-8") as f:
                f.write("book_title: 'Test Book'\n")
            with open(os.path.join(input_dir, "parts-plan.json"), "w", encoding="utf-8") as f:
                json.dump({"parts": plan_no_dur}, f)
            for p in [1, 2]:
                s = (p - 1) * 10 + 1
                e = p * 10
                fname = f"Book_Part_{p:02d}_Ch{s:04d}_to_Ch{e:04d}.mp4"
                with open(os.path.join(input_dir, fname), "wb") as f:
                    f.write(b"fake")

            args = MagicMock()
            args.auth_pool = False
            args.state_file = state_file
            args.input_dir = input_dir
            args.run_id = "run-1"
            args.source_run_id = "run-1"
            args.execution_run_id = "run-1"
            args.privacy = "public"
            args.task_id = ""
            args.book_title = "Test Book"
            args.repo = "hub-google/audiobook-generator"

            mock_youtube = MagicMock()
            mock_youtube.active_account = {"slot": 1}
            # Simulate get_or_create_playlist
            mock_youtube.playlists().list().execute.return_value = {"items": []}
            mock_youtube.playlists().insert().execute.return_value = {"id": "PL_RESUMED"}

            with patch.dict(os.environ, {"HF_TOKEN": "mock_hf_token", "HF_ARCHIVE_REPO": "test/repo"}), \
                 patch("src.youtube_upload.orchestrator.download_artifact_task"), \
                 patch("src.youtube_upload.orchestrator.HuggingFaceArchiver") as mock_hf_cls, \
                 patch("src.youtube_upload.orchestrator.save_book_metadata", return_value={"cover_file": "cover.jpg", "title": "Book Title", "description": "desc"}), \
                 patch("src.youtube_upload.orchestrator.get_authenticated_service", return_value=mock_youtube), \
                 patch("src.youtube_upload.orchestrator.load_measured_prepared_part_plan") as mock_load_measured, \
                 patch("src.youtube_upload.orchestrator.get_or_create_playlist", return_value=("PL_RESUMED", True)), \
                 patch("src.youtube_upload.orchestrator.run_local_prepared_parts_mode", return_value=([], plan_no_dur, 2, 0)), \
                 patch("src.youtube_upload.orchestrator.run_final_playlist_and_archive_audit", return_value=0):

                mock_load_measured.return_value = [
                    {"part_num": 1, "start_chap": 1, "end_chap": 10, "duration": 3600.0, "title": "Book Part 01"},
                    {"part_num": 2, "start_chap": 11, "end_chap": 20, "duration": 4000.0, "title": "Book Part 02"},
                ]

                exit_code = run_upload_pipeline(args)
                self.assertEqual(exit_code, 0)
                # Verify load_measured was called even though part_plan already existed in saved_state
                mock_load_measured.assert_called_once_with(input_dir, "Test Book")

    def test_regression_4_any_part_duration_le_zero_fails(self):
        """Regression 4: If any Part duration <= 0, fails immediately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            args = MagicMock()
            args.auth_pool = False
            args.state_file = state_file
            args.input_dir = tmpdir
            args.run_id = "run-1"
            args.source_run_id = "run-1"
            args.execution_run_id = "run-1"
            args.privacy = "public"
            args.task_id = ""

            mock_youtube = MagicMock()
            mock_youtube.active_account = {"slot": 1}

            with patch.dict(os.environ, {"HF_TOKEN": "mock_hf_token", "HF_ARCHIVE_REPO": "test/repo"}), \
                 patch("src.youtube_upload.orchestrator.download_artifact_task"), \
                 patch("src.youtube_upload.orchestrator.HuggingFaceArchiver"), \
                 patch("src.youtube_upload.orchestrator.save_book_metadata", return_value={"cover_file": "cover.jpg", "title": "Book Title", "description": "desc"}), \
                 patch("src.youtube_upload.orchestrator.get_authenticated_service", return_value=mock_youtube), \
                 patch("src.youtube_upload.orchestrator.load_measured_prepared_part_plan") as mock_load_measured:

                # One part has 0 duration
                mock_load_measured.return_value = [
                    {"part_num": 1, "start_chap": 1, "end_chap": 10, "duration": 3600.0, "title": "Book Part 01"},
                    {"part_num": 2, "start_chap": 11, "end_chap": 20, "duration": 0.0, "title": "Book Part 02"},
                ]

                with self.assertRaisesRegex(RuntimeError, "缺少全部影片的實測時長"):
                    run_upload_pipeline(args)

    def test_regression_5_duration_does_not_affect_plan_fingerprint(self):
        """Regression 5: Duration differences must not change plan fingerprint."""
        plan1 = [
            {"part_num": 1, "start_chap": 1, "end_chap": 10, "chapters": list(range(1, 11)), "title": "Part 01", "duration": 123.456},
            {"part_num": 2, "start_chap": 11, "end_chap": 20, "chapters": list(range(11, 21)), "title": "Part 02", "duration": 789.012},
        ]
        plan2 = [
            {"part_num": 1, "start_chap": 1, "end_chap": 10, "chapters": list(range(1, 11)), "title": "Part 01", "duration": 9999.0},
            {"part_num": 2, "start_chap": 11, "end_chap": 20, "chapters": list(range(11, 21)), "title": "Part 02", "duration": 0.0},
        ]
        fp1 = plan_fingerprint(plan1)
        fp2 = plan_fingerprint(plan2)
        self.assertEqual(fp1, fp2)
        # Normalized plan must exclude duration
        self.assertNotIn("duration", normalized_plan(plan1)[0])

    def test_regression_6_remote_evidence_only_recovers_proven_steps(self):
        """Regression 6: In final_audit, YouTube video existence only recovers upload_video and add_playlist.

        Local steps (e.g. validate_video, generate_subtitle) must NOT be marked complete by YouTube existence.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            plan = self._sample_plan(1)
            checkpoint = PublicationCheckpoint(state_file)
            checkpoint.lock_plan(plan, run_id="run-1", book_title="Test Book")

            # Intentionally do NOT complete validate_video or generate_subtitle
            # Checkpoint knows video_id
            checkpoint.data["parts"]["1"]["steps"]["upload_video"]["youtube_video_id"] = "vid-1"
            checkpoint.data["parts"]["1"]["upload"]["video_id"] = "vid-1"

            mock_youtube = MagicMock()
            mock_items = [
                {
                    "id": "pli-1",
                    "snippet": {
                        "title": "Book Part 01",
                        "position": 0,
                        "resourceId": {"videoId": "vid-1"},
                    },
                }
            ]
            mock_youtube.playlistItems().list().execute.return_value = {"items": mock_items, "nextPageToken": None}

            mock_hf = MagicMock()
            mock_hf.verify_book.return_value = {"parts": 1, "repo_id": "test/repo", "book_root": "book"}
            mock_hf.completed_parts.return_value = {1}
            mock_hf_executor = MagicMock()

            args = MagicMock()
            args.state_file = state_file
            args.run_id = "run-1"
            args.privacy = "public"

            exit_code = run_final_playlist_and_archive_audit(
                mock_youtube, checkpoint, mock_hf, mock_hf_executor, args, "PL_TEST",
                "desc", {"Book Part 01"}, plan,
                {}, {}, {}, {}, "Test Book", "test/repo", 1,
            )
            self.assertEqual(exit_code, 0)

            part_steps = checkpoint.data["parts"]["1"]["steps"]
            # upload_video and add_playlist and archive_hf recovered:
            self.assertEqual(part_steps["upload_video"]["status"], "completed")
            self.assertEqual(part_steps["add_playlist"]["status"], "completed")
            self.assertEqual(part_steps["archive_hf"]["status"], "completed")
            # Quality checks MUST NOT have been marked complete by remote evidence:
            self.assertNotEqual(part_steps["validate_video"]["status"], "completed")
            self.assertNotEqual(part_steps["generate_subtitle"]["status"], "completed")
            self.assertNotEqual(part_steps["prepare_chapters"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
