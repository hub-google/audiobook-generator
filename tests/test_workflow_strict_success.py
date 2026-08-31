from pathlib import Path
import unittest

import yaml


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "audiobook.yml"
DISPATCHER_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "queue-dispatcher.yml"
PREPARE_PARTS_PATH = Path(__file__).parents[1] / "src" / "prepare_parts.py"
UPLOADER_PATH = Path(__file__).parents[1] / "src" / "youtube_api_uploader.py"


class WorkflowStrictSuccessTests(unittest.TestCase):
    def test_manual_rerun_or_manual_resume_can_probe_youtube_early(self):
        workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("github.run_attempt > 1", workflow_text)
        self.assertIn("inputs.resume_source_run_id != ''", workflow_text)
        self.assertIn("github.triggering_actor != 'github-actions[bot]'", workflow_text)

    def test_playlist_metadata_quota_is_classified_as_retryable(self):
        uploader_text = UPLOADER_PATH.read_text(encoding="utf-8")
        self.assertIn("PAUSED during playlist metadata update", uploader_text)
        self.assertIn("PAUSED during final playlist metadata update", uploader_text)

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        parsed = yaml.safe_load(cls.text)
        cls.jobs = parsed["jobs"]
        cls.dispatcher_text = DISPATCHER_WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.dispatcher_jobs = yaml.safe_load(cls.dispatcher_text)["jobs"]
        cls.prepare_parts_text = PREPARE_PARTS_PATH.read_text(encoding="utf-8")

    def test_final_gate_depends_on_every_production_job(self):
        gate = self.jobs["strict_success_gate"]
        self.assertEqual(
            set(gate["needs"]),
            {"setup", "process_chapters", "plan_parts", "merge_parts", "upload_to_youtube"},
        )
        command = gate["steps"][0]["run"]
        self.assertIn('"$SETUP_RESULT" != "success"', command)
        self.assertIn('"$WORKERS_RESULT" != "success"', command)
        self.assertIn('"$PLAN_RESULT" != "success"', command)
        self.assertIn('"$MERGE_RESULT" != "success"', command)
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
        self.assertEqual(len(upload_steps), 3)
        self.assertTrue(all(step["with"]["if-no-files-found"] == "error" for step in upload_steps))
        self.assertTrue(all(step.get("if") == "always()" for step in upload_steps))
        mp4_step = next(step for step in upload_steps if "mp4-worker" in step["with"]["name"])
        self.assertIn("Workspace/*/SourceStatus/", mp4_step["with"]["path"])
        self.assertIn("Workspace/*/Manifests/", mp4_step["with"]["path"])
        manifest_step = next(step for step in upload_steps if "manifest-worker" in step["with"]["name"])
        self.assertIn("manifest-worker-", manifest_step["with"]["path"])

    def test_only_locked_failed_run_is_restored_before_cache(self):
        step = next(
            item for item in self.jobs["process_chapters"]["steps"]
            if item.get("name") == "Download only the locked failed Run (maximum 3 attempts)"
        )
        command = step["run"]
        self.assertIn('for attempt in 1 2 3', command)
        self.assertIn('--name "video-worker-$WORKER_ID"', command)
        self.assertNotIn("mp4-worker", command)
        steps = self.jobs["process_chapters"]["steps"]
        history_index = steps.index(step)
        cache_index = next(
            index for index, item in enumerate(steps)
            if item.get("name") == "Restore Cache only when artifact is absent or incomplete"
        )
        self.assertLess(history_index, cache_index)
        restore = next(
            item for item in steps
            if item.get("name") == "Validate and restore only the locked failed Run"
        )
        self.assertIn("--stage restore_artifact", restore["run"])
        self.assertNotIn("restore_history", self.text)

    def test_youtube_job_waits_for_plan_and_every_merge_worker(self):
        steps = self.jobs["upload_to_youtube"]["steps"]
        self.assertFalse(any("WORKER_RESULT" in str(step) for step in steps))
        self.assertEqual(
            set(self.jobs["upload_to_youtube"]["needs"]),
            {"setup", "process_chapters", "plan_parts", "merge_parts"},
        )
        self.assertIn("needs.setup.result == 'success'", self.jobs["upload_to_youtube"]["if"])
        self.assertIn("needs.merge_parts.result == 'success'", self.jobs["upload_to_youtube"]["if"])
        self.assertEqual(self.jobs["merge_parts"]["strategy"]["max-parallel"], 17)

    def test_locked_plan_and_completed_merge_shards_are_reused(self):
        plan_steps=self.jobs["plan_parts"]["steps"]
        self.assertTrue(any(step.get("id")=="restore_plan" for step in plan_steps))
        plan=next(step for step in plan_steps if step.get("id")=="plan")
        self.assertEqual(plan.get("if"),"steps.restore_plan.outcome != 'success'")
        merge_steps=self.jobs["merge_parts"]["steps"]
        self.assertTrue(any(step.get("id")=="restore_merge" for step in merge_steps))
        merge=next(step for step in merge_steps if step.get("name")=="Merge only assigned locked Parts")
        self.assertEqual(merge.get("if"),"steps.restore_merge.outcome != 'success'")

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

    def test_hf_summary_counts_only_the_current_book(self):
        summary = next(
            step["run"] for step in self.jobs["upload_to_youtube"]["steps"]
            if step.get("name") == "Generate Job Summary"
        )
        self.assertIn('book_title = publication.data.get("book_title", "")', summary)
        self.assertIn("books.get(book_title, {})", summary)
        self.assertNotIn("for book in books.values()", summary)
        self.assertIn("Current Book Hugging Face Parts", summary)

    def test_hf_receives_complete_part_archives_in_one_commit_per_worker(self):
        merge = self.jobs["merge_parts"]
        upload_steps = [step for step in merge["steps"] if step.get("uses") == "actions/upload-artifact@v4"]
        self.assertEqual(len(upload_steps), 1)
        sidecar_paths = upload_steps[0]["with"]["path"]
        self.assertIn("prepared_parts/*.srt", sidecar_paths)
        self.assertIn("prepared_parts/shard-manifest-*.json", sidecar_paths)
        self.assertNotIn("*.mp4", sidecar_paths)
        self.assertIn("CommitOperationAdd", self.prepare_parts_text)
        self.assertEqual(self.prepare_parts_text.count("api.create_commit("), 1)
        self.assertNotIn("api.upload_file(", self.prepare_parts_text)
        for archive_name in ("merge_manifest.json", "part_manifest.json", "media_info.json"):
            self.assertIn(archive_name, self.prepare_parts_text)
        self.assertIn("remote_subtitle", self.prepare_parts_text)
        fetch = next(step for step in self.jobs["upload_to_youtube"]["steps"] if step.get("name") == "Fetch and verify merge-complete Parts from HF")
        self.assertIn("--sidecar-dir prepared_sidecars", fetch["run"])


if __name__ == "__main__":
    unittest.main()
