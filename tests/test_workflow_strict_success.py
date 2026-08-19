from pathlib import Path
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "audiobook.yml"
DISPATCHER_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "queue-dispatcher.yml"


class WorkflowStrictSuccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        parsed = yaml.safe_load(cls.text)
        cls.jobs = parsed["jobs"]
        cls.dispatcher_text = DISPATCHER_WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.dispatcher_jobs = yaml.safe_load(cls.dispatcher_text)["jobs"]

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

    def test_dispatcher_runs_one_task_aware_decision(self):
        steps = self.dispatcher_jobs["dispatch_once"]["steps"]
        self.assertTrue(any("python src/queue_dispatcher.py" in step.get("run", "") for step in steps))
        self.assertIn("audiobook-queue-dispatcher", self.dispatcher_text)
        self.assertIn("workflow_run:", self.dispatcher_text)

    def test_dispatcher_always_writes_a_readable_job_summary(self):
        steps = self.dispatcher_jobs["dispatch_once"]["steps"]
        summary_step = next(step for step in steps if step.get("name") == "Write dispatcher result to job summary")
        self.assertEqual(summary_step.get("if"), "always()")
        self.assertIn("GITHUB_STEP_SUMMARY", summary_step.get("run", ""))
        self.assertIn("調度訊息", summary_step.get("run", ""))

    def test_worker_concurrency_is_capped_at_seventeen(self):
        self.assertEqual(self.jobs["process_chapters"]["strategy"]["max-parallel"], 17)

    def test_run_names_are_readable_and_dispatcher_history_is_pruned(self):
        self.assertIn("有聲小說製作", self.text)
        self.assertIn("inputs.book_title", self.text)
        self.assertIn("有聲小說佇列調度", self.dispatcher_text)
        self.assertIn("Delete older dispatcher run records", self.dispatcher_text)
        self.assertIn('actions/runs/$old_run_id', self.dispatcher_text)

    def test_schedule_is_isolated_from_manual_production_workflow(self):
        self.assertNotIn("schedule:", self.text)
        self.assertIn("schedule:", self.dispatcher_text)
        self.assertNotIn("workflow enable youtube-retry.yml", self.text)
        self.assertNotIn("workflow disable youtube-retry.yml", self.text)

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
        self.assertNotIn("Max retries", self.dispatcher_text)
        self.assertNotIn("max_attempt", self.dispatcher_text)

    def test_hf_archive_is_required_by_workflow(self):
        upload = str(self.jobs["upload_to_youtube"])
        self.assertIn("HF_ARCHIVE_REPO", upload)
        self.assertIn("hf_archive_state.json", upload)


if __name__ == "__main__":
    unittest.main()
