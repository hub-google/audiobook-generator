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
    @patch("src.worker_pipeline.inherit_historical_artifacts")
    def test_partial_local_run_is_never_reported_as_success(
        self, inherit, run_chapter, checkpoint_type
    ):
        checkpoint = Mock()
        checkpoint.incomplete_chapters.return_value = [2]
        checkpoint.source_missing_chapters.return_value = []
        checkpoint_type.return_value = checkpoint
        run_chapter.side_effect = [None, RuntimeError("tts failed")]

        with self.assertRaisesRegex(RuntimeError, "驗收失敗"):
            worker_pipeline.run_pipeline(self.config, build_parts=False)

        self.assertEqual(run_chapter.call_count, 2)
        inherit.assert_not_called()

    @patch("src.worker_pipeline.PipelineCheckpoint")
    @patch("src.worker_pipeline.run_resumable_chapter")
    @patch("src.worker_pipeline.inherit_historical_artifacts")
    def test_actions_queue_worker_inherits_history_before_processing(
        self, inherit, run_chapter, checkpoint_type
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

        inherit.assert_called_once_with(config, checkpoint, 0, [1, 2])
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

    @patch("src.youtube_api_uploader.download_artifact_task")
    def test_inherit_historical_artifacts_skips_deleted_runs_and_completes(self, download_task):
        checkpoint = Mock()
        checkpoint.workspace_dir = "/tmp/workspace"
        checkpoint.incomplete_chapters.side_effect = [[1, 2], [1, 2], []]
        checkpoint.reconcile.return_value = None

        config = dict(self.config, book_profile_id="book-fp")

        def download(run_id, _repo, artifact_name, destination):
            if str(run_id) == "999":
                return False
            os.makedirs(destination, exist_ok=True)
            if artifact_name == "shared-config":
                import yaml
                with open(os.path.join(destination, "config.yaml"), "w", encoding="utf-8") as handle:
                    yaml.safe_dump({"book_profile_id": "book-fp"}, handle)
                return True
            return artifact_name == "video-worker-0"

        download_task.side_effect = download

        with patch("src.cloud_queue.GitHubQueueStore") as mock_store_cls:
            mock_store = Mock()
            mock_store.load.return_value = ({
                "queue": [{
                    "task_id": "task-123",
                    "book_title": "book",
                    "book_profile_id": "book-fp",
                    "run_history": [
                        {"run_id": 888, "ended_at": "2026-08-01T00:00:00Z"},
                        {"run_id": 999, "ended_at": "2026-08-02T00:00:00Z"},
                    ]
                }]
            }, "sha-1")
            mock_store_cls.return_value = mock_store

            with patch.dict(os.environ, {"GH_TOKEN": "token", "QUEUE_TASK_ID": "task-123", "GITHUB_RUN_ID": "1000"}):
                worker_pipeline.inherit_historical_artifacts(config, checkpoint, 0, [1, 2])

        # Checked runs: first tried 999, then 888
        self.assertTrue(download_task.called)

    @patch("src.worker_pipeline._copy_artifact_files_to_workspace")
    @patch("src.worker_pipeline.subprocess.run")
    @patch("src.youtube_api_uploader.download_artifact_task")
    def test_fingerprint_locked_history_skips_deleted_latest_and_uses_older_run(
        self, download_task, run_command, copy_files
    ):
        import yaml

        checkpoint = Mock()
        checkpoint.workspace_dir = "/tmp/workspace"
        checkpoint.incomplete_chapters.side_effect = [[1, 2], []]
        run_command.return_value = Mock(returncode=0, stdout="", stderr="")

        def download(run_id, _repo, artifact_name, destination):
            if str(run_id) == "999":
                return False
            os.makedirs(destination, exist_ok=True)
            if artifact_name == "shared-config":
                with open(os.path.join(destination, "config.yaml"), "w", encoding="utf-8") as handle:
                    yaml.safe_dump({"book_profile_id": "book-fp"}, handle)
                return True
            return artifact_name == "video-worker-0"

        download_task.side_effect = download
        config = dict(self.config, queue_task_id="task-123", book_profile_id="book-fp")
        with patch("src.cloud_queue.GitHubQueueStore") as mock_store_cls:
            mock_store_cls.return_value.load.return_value = ({
                "queue": [{
                    "task_id": "task-123", "book_title": "book",
                    "book_profile_id": "book-fp",
                    "run_history": [{"run_id": 888}, {"run_id": 999}],
                }]
            }, "sha-1")
            with patch.dict(os.environ, {
                "GH_TOKEN": "token", "QUEUE_TASK_ID": "task-123",
                "GITHUB_RUN_ID": "1000",
            }):
                worker_pipeline.inherit_historical_artifacts(config, checkpoint, 0, [1, 2])

        calls = [(str(call.args[0]), call.args[2]) for call in download_task.call_args_list]
        self.assertEqual(calls[0], ("999", "shared-config"))
        self.assertIn(("888", "shared-config"), calls)
        self.assertIn(("888", "video-worker-0"), calls)
        copy_files.assert_called_once()
        checkpoint.reconcile.assert_called_once()

    def test_history_candidates_use_only_exact_profile_and_global_newest_order(self):
        queue = {
            "queue": [
                {"book_title": "same title", "book_profile_id": "wanted", "run_history": [
                    {"run_id": 1, "ended_at": "2026-08-01T00:00:00Z"},
                    {"run_id": 3, "ended_at": "2026-08-03T00:00:00Z"},
                ]},
                {"book_title": "same title", "book_profile_id": "wrong", "run_history": [
                    {"run_id": 99, "ended_at": "2026-08-09T00:00:00Z"},
                ]},
            ],
            "completed": [{"book_profile_id": "wanted", "run_history": [
                {"run_id": 2, "ended_at": "2026-08-02T00:00:00Z"},
            ]}],
        }
        self.assertEqual(worker_pipeline.historical_run_candidates(queue, "wanted"), [3, 2, 1])

    @patch("src.youtube_api_uploader.download_artifact_task", return_value=False)
    def test_history_exhausts_every_same_profile_run_before_regeneration(self, download_task):
        checkpoint = Mock()
        checkpoint.workspace_dir = "/tmp/workspace"
        checkpoint.incomplete_chapters.return_value = [1, 2]
        config = dict(self.config, book_profile_id="book-fp")
        history = [
            {"run_id": run_id, "ended_at": f"2026-08-{run_id:02d}T00:00:00Z"}
            for run_id in range(1, 10)
        ]
        with patch("src.cloud_queue.GitHubQueueStore") as store_type:
            store_type.return_value.load.return_value = ({
                "queue": [{"book_profile_id": "book-fp", "run_history": history}],
                "completed": [],
            }, "sha")
            with patch.dict(os.environ, {"GH_TOKEN": "token", "GITHUB_RUN_ID": "10"}):
                worker_pipeline.inherit_historical_artifacts(config, checkpoint, 0, [1, 2])

        shared_config_runs = [
            int(call.args[0]) for call in download_task.call_args_list
            if call.args[2] == "shared-config"
        ]
        self.assertEqual(shared_config_runs, [9, 8, 7, 6, 5, 4, 3, 2, 1])

    @patch("src.worker_pipeline._copy_artifact_files_to_workspace")
    @patch("src.worker_pipeline.PipelineCheckpoint")
    def test_locked_artifact_must_pass_fingerprint_and_chapter_validation(self, checkpoint_type, copy_files):
        import tempfile
        import yaml
        checkpoint = Mock()
        checkpoint.incomplete_chapters.return_value = []
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

    def test_workflow_enforces_artifact_before_conditional_cache(self):
        from pathlib import Path
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "audiobook.yml").read_text(encoding="utf-8")
        history = workflow.index("Restore same-book historical Run artifacts newest-to-oldest")
        cache = workflow.index("Restore Cache only when artifact is absent or incomplete")
        self.assertLess(history, cache)
        cache_block = workflow[cache:workflow.index("- name: Log Cache Location", cache)]
        self.assertIn("if: steps.history_restore.outputs.complete != 'true'", cache_block)
        self.assertIn(
            "${{ matrix.book_title }}-chap${{ matrix.start_chap }}-${{ matrix.end_chap }}-worker${{ matrix.worker_id }}-",
            cache_block,
        )


if __name__ == "__main__":
    unittest.main()
