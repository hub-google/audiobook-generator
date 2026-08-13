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
    def test_partial_local_run_is_never_reported_as_success(self, run_chapter, checkpoint_type):
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


if __name__ == "__main__":
    unittest.main()
