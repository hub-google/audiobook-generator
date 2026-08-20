import json
import os
import tempfile
import unittest
import hashlib
from unittest.mock import patch

from src.pipeline_checkpoint import PipelineCheckpoint, STAGES


class PipelineCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.temp_dir.name
        self.book = "test-book"
        self.validator = patch("src.pipeline_checkpoint.validate_stage", side_effect=self._validate_fixture)
        self.validator.start()

    def tearDown(self):
        self.validator.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _validate_fixture(stage, path, **kwargs):
        if not os.path.exists(path):
            from src.artifact_validation import ArtifactValidationError
            raise ArtifactValidationError("missing fixture")
        with open(path, "rb") as handle:
            payload = handle.read()
        minimum = {"crawler": 10, "cleaner": 10, "tts": 100, "subtitle": 10, "image": 100, "video": 1000}[stage]
        if len(payload) <= minimum:
            from src.artifact_validation import ArtifactValidationError
            raise ArtifactValidationError("fixture too short")
        return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

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

    def test_confirmed_source_missing_is_terminal_success_with_warning(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 11, [721])
        checkpoint.mark_source_missing(721, "origin returned three matching empty pages")

        restored = PipelineCheckpoint(self.workspace, self.book, 11, [721])

        self.assertEqual(restored.incomplete_chapters(), [])
        self.assertEqual(restored.source_missing_chapters(), [721])
        self.assertEqual(restored.data["chapters"]["721"]["overall_status"], "source_missing")
        self.assertIn("Origin website missing: **1**", restored.markdown_summary())

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

    def test_worker_stage_accepts_one_path_without_splitting_characters(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])
        output = os.path.join(self.workspace, "part-1.mp4")
        with open(output, "wb") as part_file:
            part_file.write(b"video")

        checkpoint.mark_worker_stage_completed("part_build", output)

        self.assertEqual(
            checkpoint.data["worker_stages"]["part_build"]["outputs"],
            [os.path.abspath(output)],
        )

    def test_worker_stage_rejects_non_path_outputs_clearly(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])

        with self.assertRaisesRegex(TypeError, "outputs must be file paths"):
            checkpoint.mark_worker_stage_completed(
                "part_build", [{"merged_video": "part-1.mp4"}]
            )

    def test_upstream_hash_change_invalidates_downstream_outputs(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1])
        for stage in STAGES:
            self._write_output(checkpoint, 1, stage)
        checkpoint.reconcile()
        clean_path = checkpoint.output_path(1, "cleaner")
        with open(clean_path, "a", encoding="utf-8") as handle:
            handle.write("changed")

        restored = PipelineCheckpoint(self.workspace, self.book, 0, [1])

        self.assertEqual(restored.data["chapters"]["1"]["stages"]["cleaner"]["status"], "completed")
        self.assertEqual(restored.data["chapters"]["1"]["stages"]["tts"]["status"], "pending")
        self.assertIn("upstream artifact changed", restored.data["chapters"]["1"]["stages"]["tts"]["validation_error"])

    def test_export_manifest_creates_valid_lightweight_json(self):
        checkpoint = PipelineCheckpoint(self.workspace, self.book, 0, [1, 2])
        for stage in STAGES:
            self._write_output(checkpoint, 1, stage)
        checkpoint.mark_source_missing(2, "missing from origin")
        checkpoint.reconcile()

        manifest = checkpoint.export_manifest()
        self.assertEqual(manifest["worker_id"], 0)
        self.assertEqual(manifest["artifact"], "mp4-worker-0")
        self.assertEqual(len(manifest["chapters"]), 1)
        self.assertEqual(manifest["chapters"][0]["chap_num"], 1)
        self.assertEqual(manifest["source_missing"], [2])
        manifest_path = os.path.join(self.workspace, "Manifests", "manifest-worker-0.json")
        self.assertTrue(os.path.exists(manifest_path))


if __name__ == "__main__":
    unittest.main()
