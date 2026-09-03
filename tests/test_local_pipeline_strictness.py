import os
import unittest
from unittest.mock import Mock, patch

from src import worker_pipeline


class LocalPipelineStrictnessTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "book_title": "book",
            "paths": {"workspace_base": "Workspace"},
            "chapters": ["url-1", "url-2"],
            "selected_indices": [1, 2],
        }

    @patch("src.worker_pipeline.PipelineCheckpoint")
    @patch("src.worker_pipeline.run_resumable_chapter")
    def test_partial_local_run_is_never_reported_as_success(
        self, run_chapter, checkpoint_type
    ):
        checkpoint = Mock()
        checkpoint.incomplete_chapters.return_value = [2]
        checkpoint.source_missing_chapters.return_value = []
        checkpoint_type.return_value = checkpoint
        run_chapter.side_effect = [None, RuntimeError("tts failed")]

        with self.assertRaisesRegex(RuntimeError, "驗收失敗"):
            worker_pipeline.run_pipeline(self.config, build_parts=False)

        self.assertEqual(run_chapter.call_count, 2)

    @patch("src.worker_pipeline.PipelineCheckpoint")
    @patch("src.worker_pipeline.run_resumable_chapter")
    def test_actions_worker_never_scans_queue_history_during_pipeline(
        self, run_chapter, checkpoint_type
    ):
        checkpoint = Mock()
        checkpoint.incomplete_chapters.return_value = []
        checkpoint.source_missing_chapters.return_value = []
        checkpoint_type.return_value = checkpoint
        config = dict(self.config, queue_task_id="task-123", book_profile_id="book-fp")

        with patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "1000",
            "GH_TOKEN": "token", "QUEUE_TASK_ID": "task-123",
        }):
            worker_pipeline.run_pipeline(config, build_parts=False)

        self.assertEqual(run_chapter.call_count, 2)

    @patch(
        "src.worker_pipeline.stage_video_gen",
        return_value=[{"merged_video": "part-1.mp4", "part_num": 1}],
    )
    @patch("src.worker_pipeline.PipelineCheckpoint")
    @patch("src.worker_pipeline.run_resumable_chapter")
    def test_part_build_only_runs_after_every_chapter_passes(
        self, run_chapter, checkpoint_type, build_parts
    ):
        checkpoint = Mock()
        checkpoint.incomplete_chapters.return_value = []
        checkpoint.source_missing_chapters.return_value = []
        checkpoint_type.return_value = checkpoint

        result = worker_pipeline.run_pipeline(self.config, build_parts=True)

        self.assertIs(result, checkpoint)
        self.assertEqual(run_chapter.call_count, 2)
        checkpoint.mark_worker_stage_running.assert_called_once_with("part_build")
        checkpoint.mark_worker_stage_completed.assert_called_once_with(
            "part_build", ["part-1.mp4"]
        )

    def test_part_output_paths_rejects_missing_merged_video(self):
        with self.assertRaisesRegex(RuntimeError, "no merged video path"):
            worker_pipeline.part_output_paths([{"part_num": 1, "merged_video": None}])

    @patch("src.worker_pipeline.stage_video_gen")
    @patch("src.worker_pipeline.stage_image_gen")
    @patch("src.worker_pipeline.stage_tts")
    @patch("src.worker_pipeline.stage_clean")
    @patch("src.worker_pipeline.stage_crawl")
    def test_fully_valid_locked_worker_runs_zero_production_stages(
        self, crawl, clean, tts, image, video,
    ):
        checkpoint = Mock()
        checkpoint.is_completed.return_value = True

        worker_pipeline.run_resumable_chapter(
            self.config, checkpoint, "url-1", 1, worker_id=0,
        )

        crawl.assert_not_called()
        clean.assert_not_called()
        tts.assert_not_called()
        image.assert_not_called()
        video.assert_not_called()


    def test_copy_artifact_files_to_workspace(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp_src, tempfile.TemporaryDirectory() as temp_dest:
            video_dir = os.path.join(temp_src, "Video")
            os.makedirs(video_dir, exist_ok=True)
            mp4_file = os.path.join(video_dir, "book_chapter_1.mp4")
            with open(mp4_file, "wb") as handle:
                handle.write(b"mp4-content-12345")

            worker_pipeline._copy_artifact_files_to_workspace(temp_src, temp_dest, "book")
            copied_file = os.path.join(temp_dest, "Video", "book_chapter_1.mp4")
            self.assertTrue(os.path.exists(copied_file))
            self.assertEqual(os.path.getsize(copied_file), len(b"mp4-content-12345"))

    @patch("src.worker_pipeline._copy_artifact_files_to_workspace")
    @patch("src.worker_pipeline.PipelineCheckpoint")
    def test_locked_artifact_must_pass_fingerprint_and_chapter_validation(self, checkpoint_type, copy_files):
        import tempfile
        import yaml
        checkpoint = Mock()
        checkpoint.incomplete_chapters.return_value = []
        checkpoint.source_missing_chapters.return_value = []
        checkpoint.validate_manifest.return_value = {"chapter_count": 2, "total_duration_seconds": 120.0, "missing_count": 0}
        checkpoint_type.return_value = checkpoint
        config = dict(self.config, book_profile_id="fingerprint-1")
        with tempfile.TemporaryDirectory() as root:
            artifact_dir = os.path.join(root, "artifact")
            os.makedirs(artifact_dir)
            with open(os.path.join(artifact_dir, "chapter_1.mp4"), "wb") as handle:
                handle.write(b"artifact")
            source_config = os.path.join(root, "config.yaml")
            with open(source_config, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"book_profile_id": "fingerprint-1"}, handle)
            self.assertTrue(worker_pipeline.restore_locked_artifact(
                config, 0, [1, 2], artifact_dir, source_config, "123"
            ))
        copy_files.assert_called_once()
        checkpoint.reconcile.assert_called_once()
        checkpoint.export_manifest.assert_called_once()
        checkpoint.validate_manifest.assert_called_once()

    def test_locked_artifact_rejects_wrong_book_fingerprint(self):
        import tempfile
        import yaml
        config = dict(self.config, book_profile_id="expected")
        with tempfile.TemporaryDirectory() as root:
            artifact_dir = os.path.join(root, "artifact")
            os.makedirs(artifact_dir)
            with open(os.path.join(artifact_dir, "chapter_1.mp4"), "wb") as handle:
                handle.write(b"artifact")
            source_config = os.path.join(root, "config.yaml")
            with open(source_config, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"book_profile_id": "wrong"}, handle)
            with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
                worker_pipeline.restore_locked_artifact(
                    config, 0, [1, 2], artifact_dir, source_config, "123"
                )

    def test_artifact_restore_preserves_worker_checkpoint_signatures(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "artifact", "book", "Checkpoints")
            workspace = os.path.join(root, "workspace")
            os.makedirs(artifact)
            checkpoint = {
                "schema_version": 2,
                "book_title": "book",
                "worker_id": 0,
                "chapters": {},
                "worker_stages": {},
            }
            source = os.path.join(artifact, "worker-0.json")
            with open(source, "w", encoding="utf-8") as handle:
                json.dump(checkpoint, handle)

            worker_pipeline._copy_artifact_files_to_workspace(
                os.path.join(root, "artifact"), workspace, "book"
            )

            restored = os.path.join(workspace, "Checkpoints", "worker-0.json")
            self.assertTrue(os.path.isfile(restored))
            with open(restored, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["worker_id"], 0)

    def test_locked_checkpoint_overwrites_larger_local_placeholder(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            artifact = os.path.join(root, "artifact", "book", "Checkpoints")
            workspace_checkpoint = os.path.join(root, "workspace", "Checkpoints")
            os.makedirs(artifact)
            os.makedirs(workspace_checkpoint)
            source = os.path.join(artifact, "worker-0.json")
            destination = os.path.join(workspace_checkpoint, "worker-0.json")
            with open(source, "w", encoding="utf-8") as handle:
                json.dump({"worker_id": 0, "signature": "trusted"}, handle)
            with open(destination, "w", encoding="utf-8") as handle:
                json.dump({"worker_id": 0, "placeholder": "x" * 5000}, handle)

            worker_pipeline._copy_artifact_files_to_workspace(
                os.path.join(root, "artifact"), os.path.join(root, "workspace"), "book",
            )

            with open(destination, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["signature"], "trusted")

    def test_workflow_enforces_artifact_before_conditional_cache(self):
        from pathlib import Path
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "audiobook.yml").read_text(encoding="utf-8")
        history = workflow.index("Download only the locked failed Run (maximum 3 attempts)")
        cache = workflow.index("Restore Cache only when artifact is absent or incomplete")
        self.assertLess(history, cache)
        cache_block = workflow[cache:workflow.index("- name: Log Cache Location", cache)]
        self.assertIn("if: steps.artifact_restore.outputs.complete != 'true'", cache_block)
        self.assertIn('for attempt in 1 2 3', workflow)
        self.assertNotIn("restore_history", workflow)
        self.assertNotIn("mp4-worker-$WORKER_ID", workflow)
        self.assertIn(
            "${{ matrix.book_title }}-chap${{ matrix.start_chap }}-${{ matrix.end_chap }}-worker${{ matrix.worker_id }}-",
            cache_block,
        )


if __name__ == "__main__":
    unittest.main()
