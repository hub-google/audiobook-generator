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

        # Simulate Run 999 failing/deleted (False), Run 888 succeeding (True)
        download_task.side_effect = [False, False, True, True]

        with patch("src.cloud_queue.GitHubQueueStore") as mock_store_cls:
            mock_store = Mock()
            mock_store.load.return_value = ({
                "tasks": [{
                    "task_id": "task-123",
                    "book_title": "book",
                    "run_history": [
                        {"run_id": 888, "conclusion": "failure"},
                        {"run_id": 999, "conclusion": "failure"},
                    ]
                }]
            }, "sha-1")
            mock_store_cls.return_value = mock_store

            with patch.dict(os.environ, {"GH_TOKEN": "token", "QUEUE_TASK_ID": "task-123", "GITHUB_RUN_ID": "1000"}):
                worker_pipeline.inherit_historical_artifacts(self.config, checkpoint, 0, [1, 2])

        # Checked runs: first tried 999, then 888
        self.assertTrue(download_task.called)


if __name__ == "__main__":
    unittest.main()
