import json
import os
import tempfile
import unittest

from src.success_criteria import validate_upload_success
from src.publication_checkpoint import GLOBAL_STEPS, PART_STEPS


class StrictSuccessCriteriaTests(unittest.TestCase):
    def write_evidence(self, directory, *, status="complete", completed=True, pending=False):
        state_path = os.path.join(directory, "state.json")
        plan = [{"part_num": 1, "title": "Book Part 01"}]
        state = {
            "run_id": "123",
            "status": status,
            "part_plan": plan,
            "completed_titles": ["Book Part 01"] if completed else [],
            "pending_thumbnails": {"Book Part 01": "v1"} if pending else {},
            "pending_playlist": {},
            "pending_captions": {},
            "pending_publish": {},
            "playlist_url": "https://www.youtube.com/playlist?list=PL123",
        }
        execution = {
            "source_run_id": "123",
            "plan_status": "locked",
            "global_steps": {step: {"status": "completed"} for step in GLOBAL_STEPS},
            "parts": {"1": {
                "overall_status": "completed",
                "steps": {step: {"status": "completed"} for step in PART_STEPS},
            }},
        }
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        with open(os.path.join(directory, "part_execution.json"), "w", encoding="utf-8") as handle:
            json.dump(execution, handle)
        return state_path

    def test_accepts_only_complete_matching_publication_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_evidence(directory)
            result = validate_upload_success(state_path, expected_run_id="123")
            self.assertEqual(result["parts"], 1)

    def test_rejects_paused_or_incomplete_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_evidence(directory, status="paused", completed=False)
            with self.assertRaisesRegex(RuntimeError, "status is 'paused'"):
                validate_upload_success(state_path, expected_run_id="123")

    def test_rejects_pending_mandatory_youtube_work(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_evidence(directory, pending=True)
            with self.assertRaisesRegex(RuntimeError, "pending_thumbnails is not empty"):
                validate_upload_success(state_path, expected_run_id="123")

    def test_rejects_wrong_source_run(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_evidence(directory)
            with self.assertRaisesRegex(RuntimeError, "does not match source run"):
                validate_upload_success(state_path, expected_run_id="999")

    def test_rejects_missing_hugging_face_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_evidence(directory)
            execution_path = os.path.join(directory, "part_execution.json")
            with open(execution_path, encoding="utf-8") as handle:
                execution = json.load(handle)
            execution["parts"]["1"]["steps"]["archive_hf"]["status"] = "failed"
            with open(execution_path, "w", encoding="utf-8") as handle:
                json.dump(execution, handle)
            with self.assertRaisesRegex(RuntimeError, "archive_hf"):
                validate_upload_success(state_path, expected_run_id="123")


if __name__ == "__main__":
    unittest.main()
