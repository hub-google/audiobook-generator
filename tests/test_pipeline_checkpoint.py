import json
import os
import tempfile
import unittest

from src.pipeline_checkpoint import PipelineCheckpoint, STAGES


class PipelineCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.temp_dir.name
        self.book = "test-book"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_output(self, checkpoint, chapter, stage):
        path = checkpoint.output_path(chapter, stage)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        minimum = {
            "crawler": 10, "cleaner": 10, "tts": 100,
            "subtitle": 10, "image": 100, "video": 1000,
        }[stage]
        mode = "wb" if stage in ("tts", "image", "video") else "w"
        kwargs = {} if "b" in mode else {"encoding": "utf-8"}
        with open(path, mode, **kwargs) as output:
            payload = b"x" * (minimum + 1) if "b" in mode else "x" * (minimum + 1)
            output.write(payload)

    def test_files_rebuild_a_missing_checkpoint(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])
        for stage in STAGES:
            self._write_output(checkpoint, 1, stage)

        rebuilt = PipelineCheckpoint(self.workspace, self.book, 0, [1])

        self.assertEqual(rebuilt.incomplete_chapters(), [])
        for stage in STAGES:
            self.assertEqual(
                rebuilt.data["chapters"]["1"]["stages"][stage]["status"],
                "completed",
            )

    def test_missing_file_overrides_completed_ledger_state(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])
        for stage in STAGES:
            self._write_output(checkpoint, 1, stage)
        checkpoint.reconcile()
        os.remove(checkpoint.output_path(1, "tts"))

        restored = PipelineCheckpoint(self.workspace, self.book, 0, [1])

        self.assertEqual(restored.incomplete_chapters(), [1])
        self.assertEqual(
            restored.data["chapters"]["1"]["stages"]["tts"]["status"],
            "pending",
        )

    def test_corrupt_ledger_is_rebuilt_from_outputs(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])
        self._write_output(checkpoint, 1, "crawler")
        with open(checkpoint.path, "w", encoding="utf-8") as ledger:
            ledger.write("{broken")

        restored = PipelineCheckpoint(self.workspace, self.book, 0, [1])

        self.assertEqual(
            restored.data["chapters"]["1"]["stages"]["crawler"]["status"],
            "completed",
        )
        self.assertTrue(os.path.exists(checkpoint.path + ".corrupt"))

    def test_failure_records_resume_stage_and_reason(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [7])
        checkpoint.mark_running(7, "crawler")
        checkpoint.mark_failed(7, "crawler", RuntimeError("network stopped"))

        with open(checkpoint.path, encoding="utf-8") as ledger:
            saved = json.load(ledger)

        chapter = saved["chapters"]["7"]
        self.assertEqual(chapter["overall_status"], "failed")
        self.assertEqual(chapter["resume_from"], "crawler")
        self.assertEqual(chapter["stages"]["crawler"]["attempts"], 1)
        self.assertIn("network stopped", checkpoint.markdown_summary())

    def test_worker_level_failure_prevents_completed_summary(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])
        for stage in STAGES:
            self._write_output(checkpoint, 1, stage)
        checkpoint.reconcile()
        checkpoint.mark_worker_stage_running("part_build")
        checkpoint.mark_worker_stage_failed("part_build", RuntimeError("no part produced"))

        summary = checkpoint.markdown_summary()

        self.assertIn("Overall: **FAILED**", summary)
        self.assertIn("**part_build**: no part produced", summary)


if __name__ == "__main__":
    unittest.main()
