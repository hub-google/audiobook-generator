import unittest
from unittest.mock import Mock

from src.cover_rerun_controller import (
    MAX_NATIVE_COVER_RERUNS,
    decide_rerun,
    failed_cover_job_ids,
    run_controller,
)


class CoverRerunControllerTests(unittest.TestCase):
    def test_completed_failure_reruns_only_failed_cover_jobs(self):
        run = {
            "status": "completed",
            "conclusion": "failure",
            "name": "【封面與Gemini】測試小說｜book-1",
            "run_attempt": 1,
        }
        jobs = [
            {"id": 101, "name": "📚 Generate Gemini book introduction", "conclusion": "success"},
            {"id": 102, "name": "🧠 Generate Gemini cover information", "conclusion": "failure"},
            {"id": 103, "name": "unrelated", "conclusion": "failure"},
        ]

        decision = decide_rerun(run, jobs)

        self.assertTrue(decision.should_rerun)
        self.assertEqual(decision.failed_job_ids, (102,))
        self.assertEqual(failed_cover_job_ids(jobs), (102,))

    def test_controller_reruns_immediately_after_each_completed_attempt(self):
        client = Mock()
        client.get_run.side_effect = [
            {"status": "completed", "conclusion": "failure", "name": "【封面與Gemini】book", "run_attempt": 1},
            {"status": "in_progress", "conclusion": None, "name": "【封面與Gemini】book", "run_attempt": 2},
            {"status": "completed", "conclusion": "success", "name": "【封面與Gemini】book", "run_attempt": 2},
        ]
        client.get_jobs.side_effect = [
            [{"id": 102, "name": "🧠 Generate Gemini cover information", "conclusion": "failure"}],
            [],
        ]

        with unittest.mock.patch("src.cover_rerun_controller.time.sleep"):
            outcome = run_controller(client, "123")

        self.assertEqual(outcome, "source_run_not_failed")
        client.rerun_failed_jobs.assert_called_once_with("123")

    def test_attempt_21_stops_without_another_native_rerun(self):
        run = {
            "status": "completed",
            "conclusion": "failure",
            "name": "【封面與Gemini】book",
            "run_attempt": MAX_NATIVE_COVER_RERUNS + 1,
        }
        jobs = [{"id": 104, "name": "🎨 Generate HF cover", "conclusion": "failure"}]

        decision = decide_rerun(run, jobs)

        self.assertFalse(decision.should_rerun)
        self.assertTrue(decision.exhausted)
        self.assertEqual(decision.reason, "cover_rerun_limit_reached")

    def test_non_cover_failure_is_not_rerun(self):
        run = {
            "status": "completed",
            "conclusion": "failure",
            "name": "【TXT抓取】book",
            "run_attempt": 1,
        }
        jobs = [{"id": 101, "name": "🧠 Generate Gemini cover information", "conclusion": "failure"}]

        decision = decide_rerun(run, jobs)

        self.assertFalse(decision.should_rerun)
        self.assertEqual(decision.reason, "not_a_cover_preflight_run")

    def test_workflow_dispatches_controller_only_from_initial_failed_attempt(self):
        from pathlib import Path

        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "cover-preflight.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("trigger_cover_rerun:", workflow)
        self.assertIn("if: failure() && github.run_attempt == 1", workflow)
        self.assertIn("gh workflow run cover-rerun.yml", workflow)
        self.assertIn('--field source_run_id="$SOURCE_RUN_ID"', workflow)


if __name__ == "__main__":
    unittest.main()
