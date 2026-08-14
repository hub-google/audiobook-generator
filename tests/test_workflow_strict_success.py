from pathlib import Path
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "audiobook.yml"


class WorkflowStrictSuccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        parsed = yaml.safe_load(cls.text)
        cls.jobs = parsed["jobs"]

    def test_final_gate_depends_on_every_production_job(self):
        gate = self.jobs["strict_success_gate"]
        self.assertEqual(
            set(gate["needs"]),
            {"setup", "process_chapters", "upload_to_youtube"},
        )
        command = gate["steps"][0]["run"]
        self.assertIn('"$SETUP_RESULT" != "success"', command)
        self.assertIn('"$WORKERS_RESULT" != "success"', command)
        self.assertIn('"$YOUTUBE_RESULT" != "success"', command)
        self.assertIn("exit 1", command)

    def test_scheduled_retry_wait_and_rerun_are_not_false_failures(self):
        command = self.jobs["retry_failed_run"]["steps"][0]["run"]
        self.assertIn('elif [ "$conclusion" = "success" ]', command)
        self.assertNotIn("STRICT SUCCESS GATE FAILED", command)
        self.assertNotIn("gate_message", command)

    def test_scheduled_cleanup_deletes_completed_checks_only(self):
        command = self.jobs["retry_failed_run"]["steps"][0]["run"]
        self.assertIn('.status == \\"completed\\"', command)
        self.assertIn('github.event_name == \'schedule\'', self.text)
        self.assertIn('gh api --method DELETE "repos/$REPOSITORY/actions/runs/$stale_run_id"', command)

    def test_worker_artifacts_cannot_be_empty(self):
        steps = self.jobs["process_chapters"]["steps"]
        upload_steps = [step for step in steps if step.get("uses") == "actions/upload-artifact@v4"]
        self.assertEqual(len(upload_steps), 2)
        self.assertTrue(all(step["with"]["if-no-files-found"] == "error" for step in upload_steps))
        self.assertTrue(all(step.get("if") == "always()" for step in upload_steps))
        self.assertIn("Workspace/*/SourceStatus/", upload_steps[0]["with"]["path"])

    def test_youtube_job_can_inspect_partial_worker_artifacts(self):
        steps = self.jobs["upload_to_youtube"]["steps"]
        self.assertFalse(any("WORKER_RESULT" in str(step) for step in steps))
        self.assertEqual(
            set(self.jobs["upload_to_youtube"]["needs"]),
            {"setup", "process_chapters"},
        )
        self.assertIn("needs.setup.result == 'success'", self.jobs["upload_to_youtube"]["if"])

    def test_matrix_validation_imports_yaml(self):
        step = next(
            item for item in self.jobs["setup"]["steps"]
            if item.get("name") == "Validate non-empty chapter matrix"
        )
        self.assertIn("import yaml", step["run"])
        self.assertIn('.include | type == "array"', step["run"])
        self.assertIn('.get("include")', step["run"])

    def test_scheduled_retry_has_no_attempt_limit(self):
        command = self.jobs["retry_failed_run"]["steps"][0]["run"]
        self.assertNotIn("Max retries", command)
        self.assertNotIn('run_attempt:-1}" -ge', command)


if __name__ == "__main__":
    unittest.main()
