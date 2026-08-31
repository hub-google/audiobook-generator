import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from src.cloud_queue import (
    add_tasks, current_task, empty_queue, format_chapter_label, move_chapter_order, move_task, move_tasks, new_task, next_task,
    mark_task_interrupted, mark_task_needs_attention, requeue_task_after_active, settle_interrupted_task, update_task,
    update_task_chapters, normalize_chapter_order, normalize_queue,
)
from src.queue_dispatcher import Dispatcher, artifact_source_run_id, failed_artifact_source_candidates


class CloudQueueTests(unittest.TestCase):
    def test_artifact_source_is_latest_failed_run_with_same_stable_book_fingerprint(self):
        queue = {
            "queue": [
                {"task_id": "current", "book_profile_id": "book-fp", "run_history": [
                    {"run_id": 100, "conclusion": "failure", "ended_at": "2026-08-20T00:00:00Z"},
                    {"run_id": 300, "conclusion": "failure", "ended_at": "2026-08-22T00:00:00Z"},
                    {"run_id": 400, "conclusion": "cancelled", "ended_at": "2026-08-24T00:00:00Z"},
                ]},
                {"task_id": "other-book", "book_profile_id": "other-fp", "run_history": [
                    {"run_id": 999, "conclusion": "failure", "ended_at": "2026-08-23T00:00:00Z"},
                ]},
            ],
            "completed": [{"task_id": "older-task", "book_profile_id": "book-fp", "run_history": [
                {"run_id": 200, "conclusion": "failure", "ended_at": "2026-08-21T00:00:00Z"},
            ]}],
        }
        self.assertEqual(artifact_source_run_id(queue, "book-fp", "current"), 400)

    def test_checkpoint_source_candidates_include_cancelled_but_exclude_other_states(self):
        queue = {"queue": [{
            "task_id": "current", "book_profile_id": "book-fp", "run_history": [
                {"run_id": 10, "conclusion": "failure", "ended_at": "2026-08-20T00:00:00Z"},
                {"run_id": 20, "conclusion": "success", "ended_at": "2026-08-21T00:00:00Z"},
                {"run_id": 30, "conclusion": "cancelled", "ended_at": "2026-08-22T00:00:00Z"},
                {"run_id": 40, "conclusion": "missing", "ended_at": "2026-08-23T00:00:00Z"},
                {"run_id": 50, "conclusion": None, "ended_at": "2026-08-24T00:00:00Z"},
            ],
        }], "completed": []}
        self.assertEqual(failed_artifact_source_candidates(queue, "book-fp", "current"), [30, 10])

    def test_dispatcher_accepts_cancelled_run_when_worker_artifacts_exist(self):
        queue = {"queue": [{
            "task_id": "current", "book_profile_id": "book-fp", "run_history": [
                {"run_id": 300, "conclusion": "cancelled", "ended_at": "2026-08-22T00:00:00Z"},
            ],
        }], "completed": []}
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.run_by_id = Mock(return_value={
            "id": 300, "status": "completed", "conclusion": "cancelled",
        })
        dispatcher.run_artifact_names = Mock(return_value={
            "shared-config", "video-worker-0", "video-worker-1",
        })

        self.assertEqual(
            dispatcher.select_artifact_source_run_id(queue, "book-fp", "current"), 300,
        )

    def test_dispatcher_skips_deleted_or_unusable_failed_runs_and_locks_one_source(self):
        queue = {"queue": [{
            "task_id": "current", "book_profile_id": "book-fp", "run_history": [
                {"run_id": 100, "conclusion": "failure", "ended_at": "2026-08-20T00:00:00Z"},
                {"run_id": 200, "conclusion": "failure", "ended_at": "2026-08-21T00:00:00Z"},
                {"run_id": 300, "conclusion": "failure", "ended_at": "2026-08-22T00:00:00Z"},
            ],
        }], "completed": []}
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.run_by_id = Mock(side_effect=lambda run_id: {
            300: None,
            200: {"id": 200, "status": "completed", "conclusion": "failure"},
            100: {"id": 100, "status": "completed", "conclusion": "failure"},
        }[run_id])
        dispatcher.run_artifact_names = Mock(side_effect=lambda run_id: {
            200: {"shared-config"},
            100: {"shared-config", "video-worker-0"},
        }[run_id])

        selected = dispatcher.select_artifact_source_run_id(queue, "book-fp", "current")

        self.assertEqual(selected, 100)
        self.assertEqual(dispatcher.run_by_id.call_args_list, [
            unittest.mock.call(300), unittest.mock.call(200), unittest.mock.call(100),
        ])

    def test_normalized_number_overrides_follow_stable_uuid(self):
        task = new_task(
            "https://example/1", "第一部", 1, 2000,
            chapter_normalized_number_overrides={"1698": "1782.50"},
        )
        self.assertEqual(task["chapter_normalized_number_overrides"], {"1698": "1782.5"})
        queue = add_tasks(empty_queue(), [task])
        queue = update_task_chapters(
            queue, task["task_id"], 1, 2000,
            chapter_normalized_number_overrides={"1698": "1888.25"},
        )
        self.assertEqual(
            queue["queue"][0]["chapter_normalized_number_overrides"], {"1698": "1888.25"},
        )

    def test_schema_v1_migration_splits_completed_from_ranked_queue(self):
        pending = new_task("https://example/1", "完美世界")
        pending["position"] = 3
        completed = new_task("https://example/2", "修真聊天群")
        completed.update({"position": 2, "status": "completed"})

        migrated = normalize_queue({
            "schema_version": 1,
            "revision": 10,
            "tasks": [completed, pending],
        })

        self.assertEqual(migrated["schema_version"], 2)
        self.assertNotIn("tasks", migrated)
        self.assertEqual(migrated["queue"][0]["book_title"], "完美世界")
        self.assertEqual(migrated["queue"][0]["position"], 1)
        self.assertEqual(migrated["completed"][0]["book_title"], "修真聊天群")
        self.assertNotIn("position", migrated["completed"][0])

    def test_chapter_title_overrides_are_persisted_by_stable_uuid(self):
        task = new_task(
            "https://example/1", "第一部", 1, 3,
            chapter_title_overrides={"2": "第二章 修正"},
        )
        self.assertEqual(task["chapter_title_overrides"], {"2": "第二章 修正"})
        queue = add_tasks(empty_queue(), [task])
        queue = update_task_chapters(
            queue, task["task_id"], 1, 3,
            chapter_title_overrides={"2": "第二章 再修正"},
        )
        self.assertEqual(queue["queue"][0]["chapter_title_overrides"], {"2": "第二章 再修正"})

    def test_duplicate_chapter_count_is_persisted_and_updated(self):
        task = new_task(
            "https://example/1", "第一部", 1, 100,
            duplicate_chapter_count=7,
        )
        self.assertEqual(task["duplicate_chapter_count"], 7)

        queue = add_tasks(empty_queue(), [task])
        queue = update_task_chapters(
            queue, task["task_id"], 1, 80,
            duplicate_chapter_count=9,
        )
        self.assertEqual(queue["queue"][0]["duplicate_chapter_count"], 9)

    def test_chapter_plan_update_normalizes_exclusions(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])

        queue = update_task_chapters(queue, task["task_id"], 10, 20, [1, 10, 12, 12, 30])

        edited = queue["queue"][0]
        self.assertEqual((edited["start_chapter"], edited["end_chapter"]), (10, 20))
        self.assertEqual(edited["excluded_chapters"], [10, 12])

    def test_chapter_plan_persists_selected_chapter_renumbering(self):
        task = new_task("https://example/1", "第一部", 1, 5)
        queue = add_tasks(empty_queue(), [task])

        queue = update_task_chapters(
            queue, task["task_id"], 1, 5, [2, 4], renumber_selected=True,
        )

        edited = queue["queue"][0]
        self.assertEqual(edited["excluded_chapters"], [2, 4])
        self.assertTrue(edited["renumber_selected"])

    def test_chapter_plan_persists_actual_production_order(self):
        task = new_task(
            "https://example/1", "第一部", 1, 5,
            chapter_order=[3, 1, 5, 2, 4],
        )
        self.assertEqual(task["chapter_order"], [3, 1, 5, 2, 4])
        queue = add_tasks(empty_queue(), [task])

        queue = update_task_chapters(
            queue, task["task_id"], 1, 5,
            chapter_order=[5, 3, 1, 4, 2],
        )
        self.assertEqual(queue["queue"][0]["chapter_order"], [5, 3, 1, 4, 2])

    def test_chapter_order_discards_invalid_duplicate_and_out_of_range_values(self):
        self.assertEqual(
            normalize_chapter_order(["3", 3, 0, -1, "bad", None, 6, 2], 1, 5),
            [3, 2],
        )
        task = new_task(
            "https://example/1", "第一部", 2, 5,
            chapter_order=[1, 5, 5, "3", "bad", 9],
        )
        self.assertEqual(task["chapter_order"], [5, 3])

    def test_normalize_queue_repairs_legacy_chapter_order(self):
        task = new_task("https://example/1", "第一部", 1, 5)
        task["chapter_order"] = [3, 3, 0, "bad", 6, 1]
        queue = normalize_queue({"queue": [task], "completed": []})
        self.assertEqual(queue["queue"][0]["chapter_order"], [3, 1])

    def test_move_chapter_order_preserves_multi_selection_order(self):
        order = [1, 2, 3, 4, 5, 6]
        self.assertEqual(move_chapter_order(order, [2, 4], -1), [2, 1, 4, 3, 5, 6])
        self.assertEqual(move_chapter_order(order, [2, 4], 1), [1, 3, 2, 5, 4, 6])
        self.assertEqual(move_chapter_order(order, [2, 3], -1), [2, 3, 1, 4, 5, 6])
        self.assertEqual(move_chapter_order(order, [2, 3], 1), [1, 4, 2, 3, 5, 6])

    def test_active_chapter_plan_update_waits_for_cancel_before_requeue(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running", run_id=123)

        queue = update_task_chapters(queue, task["task_id"], 20, 80, [], requeue_after_cancel=True)

        edited = queue["queue"][0]
        self.assertEqual(edited["status"], "canceling")
        self.assertTrue(edited["requeue_after_edit"])

    def test_order_and_active_task_gate(self):
        first = new_task("https://example/1", "第一部", 1, 100)
        second = new_task("https://example/2", "第二部", 1, 200)
        queue = add_tasks(empty_queue(), [first, second])
        self.assertEqual(next_task(queue)["task_id"], first["task_id"])
        queue = update_task(queue, first["task_id"], status="waiting_retry")
        self.assertEqual(current_task(queue)["task_id"], first["task_id"])
        self.assertIsNone(next_task(queue))

    def test_failed_task_needing_attention_blocks_the_next_book(self):
        failed = new_task("https://example/1", "失敗書")
        waiting = new_task("https://example/2", "下一本")
        queue = add_tasks(empty_queue(), [failed, waiting])
        queue = update_task(queue, failed["task_id"], status="needs_attention", run_id=123,
                            run_conclusion="failure")

        self.assertEqual(current_task(queue)["task_id"], failed["task_id"])
        self.assertIsNone(next_task(queue))

    @patch("src.queue_dispatcher.subprocess.run")
    def test_huggingface_429_is_classified_for_automatic_retry(self, run):
        run.return_value = Mock(
            stdout="reason=$(python read_state.py)\n429 Too Many Requests: exceeded the rate limit for repository commits",
            stderr="",
        )
        dispatcher = Dispatcher("owner/repo", "token")

        reason, retry_at = dispatcher.retry_marker(123)

        self.assertEqual(reason, "rateLimitExceeded")
        self.assertIsNotNone(retry_at)

    @patch("src.queue_dispatcher.subprocess.run")
    def test_progress_markers_falls_back_to_job_log_on_too_many_api_requests(self, run):
        run.return_value = Mock(stdout="", stderr="too many API requests needed to fetch logs", returncode=1)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.run_jobs = Mock(return_value=[
            {"id": 999, "name": "Ordered HF + YouTube publication"},
        ])
        dispatcher.job_log = Mock(return_value="""
[API_UPLOAD_MARKER] DONE | Part 1 | Ch 1-50 | title
[API_UPLOAD_MARKER] DONE | Part 2 | Ch 51-100 | title
[HF_MEDIA_MARKER] DONE | Part 1 | title
[HF_MEDIA_MARKER] DONE | Part 2 | title
Part 1/2 | Ch 1-50
Part 2/2 | Ch 51-100
""")
        markers = dispatcher.progress_markers(123)
        self.assertIsNotNone(markers)
        self.assertEqual(markers["youtube_progress"], {"completed": 2, "total": 2})
        self.assertEqual(markers["hf_progress"], {"completed": 2, "total": 2})

    @patch("src.queue_dispatcher.subprocess.run")
    def test_progress_markers_returns_none_when_no_text(self, run):
        run.return_value = Mock(stdout="", stderr="too many API requests", returncode=1)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.run_jobs = Mock(return_value=[])
        markers = dispatcher.progress_markers(123)
        self.assertIsNone(markers)

    def test_reconcile_backfills_completed_task_with_zero_progress(self):
        task = new_task("https://example/1", "已完成零進度書")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"], status="completed", run_id=123,
            hf_progress={"completed": 0, "total": 0},
            youtube_progress={"completed": 0, "total": 0},
        )
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[])
        dispatcher.progress_markers = Mock(return_value={
            "youtube_progress": {"completed": 24, "total": 24},
            "hf_progress": {"completed": 24, "total": 24},
        })

        reconciled, changed = dispatcher.reconcile(queue)
        self.assertTrue(changed)
        comp = reconciled["completed"][0]
        self.assertEqual(comp["youtube_progress"], {"completed": 24, "total": 24})
        self.assertEqual(comp["hf_progress"], {"completed": 24, "total": 24})

    def test_reconcile_does_not_keep_postponing_an_existing_retry(self):
        task = new_task("https://example/1", "限流書")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"], status="waiting_retry", run_id=123,
            retry_at="2026-08-20T09:30:00+00:00", reason="rateLimitExceeded",
        )
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[{
            "id": 123, "status": "completed", "conclusion": "failure",
            "created_at": "2026-08-20T07:00:00Z", "updated_at": "2026-08-20T07:27:43Z",
            "display_title": f"有聲小說｜{task['task_id']}｜Ch1-100",
        }])
        dispatcher.retry_marker = Mock()
        dispatcher.progress_markers = Mock(return_value=None)

        reconciled, changed = dispatcher.reconcile(queue)

        self.assertFalse(changed)
        self.assertEqual(reconciled["queue"][0]["retry_at"], "2026-08-20T09:30:00+00:00")
        dispatcher.retry_marker.assert_not_called()

    def test_existing_retry_deadline_migrates_bad_legacy_reason_to_waiting(self):
        task = new_task("https://example/1", "舊狀態書")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"], status="needs_attention", run_id=123,
            retry_at="2026-08-20T09:55:37+00:00", reason="$(python",
        )
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[{
            "id": 123, "status": "completed", "conclusion": "failure",
            "created_at": "2026-08-20T07:00:00Z", "updated_at": "2026-08-20T07:27:43Z",
            "display_title": f"有聲小說｜{task['task_id']}｜Ch1-100",
        }])
        dispatcher.retry_marker = Mock(return_value=("$(python", None))
        dispatcher.progress_markers = Mock(return_value=None)

        reconciled, changed = dispatcher.reconcile(queue)

        self.assertTrue(changed)
        self.assertEqual(reconciled["queue"][0]["status"], "needs_attention")
        self.assertEqual(reconciled["queue"][0]["reason"], "$(python")
        self.assertIsNone(reconciled["queue"][0]["retry_at"])

    def test_paused_task_is_skipped_and_can_be_reordered(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, first["task_id"], status="paused")
        self.assertEqual(next_task(queue)["task_id"], second["task_id"])
        queue = move_task(queue, first["task_id"], 2)
        self.assertEqual(queue["queue"][1]["task_id"], first["task_id"])

    def test_active_task_cannot_be_reordered(self):
        task = new_task("https://example/1", "第一部")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running")
        with self.assertRaises(ValueError):
            move_task(queue, task["task_id"], 1)

    def test_multi_selection_moves_as_a_group_and_preserves_order(self):
        tasks = [new_task(f"https://example/{i}", f"第{i}部") for i in range(1, 6)]
        queue = add_tasks(empty_queue(), tasks)
        selected = [tasks[1]["task_id"], tasks[3]["task_id"]]

        queue = move_tasks(queue, selected, -1)
        self.assertEqual([item["book_title"] for item in queue["queue"]], [
            "第2部", "第1部", "第4部", "第3部", "第5部",
        ])

        queue = move_tasks(queue, selected, 1)
        self.assertEqual([item["book_title"] for item in queue["queue"]], [
            "第1部", "第2部", "第3部", "第4部", "第5部",
        ])

    def test_multi_selection_cannot_move_an_active_task(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, second["task_id"], status="running")

        with self.assertRaises(ValueError):
            move_tasks(queue, [first["task_id"], second["task_id"]], -1)

    def test_interrupted_task_is_non_blocking_and_requeues_after_active(self):
        interrupted = new_task("https://example/1", "第一部")
        active = new_task("https://example/2", "第二部")
        waiting = new_task("https://example/3", "第三部")
        queue = add_tasks(empty_queue(), [interrupted, active, waiting])
        queue = update_task(queue, interrupted["task_id"], status="interrupted", run_id=111)
        queue = update_task(queue, active["task_id"], status="running", run_id=222)

        self.assertEqual(current_task(queue)["task_id"], active["task_id"])
        queue = requeue_task_after_active(queue, interrupted["task_id"])

        self.assertEqual([item["task_id"] for item in queue["queue"]], [
            active["task_id"], interrupted["task_id"], waiting["task_id"],
        ])
        retried = queue["queue"][1]
        self.assertEqual(retried["status"], "queued")
        self.assertIsNone(retried["run_id"])
        self.assertEqual(retried["run_history"][0]["run_id"], 111)
        self.assertIsNotNone(retried["retry_requested_at"])

    def test_cancelled_run_interrupts_book_without_blocking_next_book(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, first["task_id"], status="running", run_id=123)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[{
            "id": 123, "status": "completed", "conclusion": "cancelled",
            "created_at": "2026-08-19T01:00:00Z", "updated_at": "2026-08-19T02:00:00Z",
            "display_title": f"有聲小說｜{first['task_id']}｜Ch1-100",
        }])
        dispatcher.progress_markers = Mock(return_value=None)

        queue, changed = dispatcher.reconcile(queue)

        self.assertTrue(changed)
        self.assertEqual(queue["queue"][0]["status"], "interrupted")
        self.assertEqual(next_task(queue)["task_id"], second["task_id"])

    def test_cancelled_edited_run_is_automatically_requeued(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="canceling", run_id=123, requeue_after_edit=True)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[{
            "id": 123, "status": "completed", "conclusion": "cancelled",
            "created_at": "2026-08-19T01:00:00Z", "updated_at": "2026-08-19T02:00:00Z",
            "display_title": f"有聲小說｜{task['task_id']}｜Ch1-100",
        }])

        queue, changed = dispatcher.reconcile(queue)

        self.assertTrue(changed)
        edited = queue["queue"][0]
        self.assertEqual(edited["status"], "queued")
        self.assertFalse(edited["requeue_after_edit"])
        self.assertIsNone(edited["run_id"])

    def test_gui_first_cancel_race_still_requeues_edited_book(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"], status="canceling", run_id=123,
            requeue_after_edit=True,
        )
        queue = mark_task_interrupted(
            queue, task["task_id"], reason="run_cancelled",
            conclusion="cancelled", ended_at="2026-08-19T02:00:00Z",
        )

        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[])
        queue, changed = dispatcher.reconcile(queue)

        self.assertTrue(changed)
        recovered = queue["queue"][0]
        self.assertEqual(recovered["status"], "queued")
        self.assertFalse(recovered["requeue_after_edit"])
        self.assertIsNone(recovered["run_id"])
        self.assertEqual(recovered["run_history"][0]["run_id"], 123)

    def test_plain_user_cancel_remains_interrupted(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running", run_id=123)

        queue = settle_interrupted_task(queue, task["task_id"])

        self.assertEqual(queue["queue"][0]["status"], "interrupted")
        self.assertEqual(queue["queue"][0]["run_id"], 123)

    def test_dispatcher_starts_next_book_after_cancelled_run(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, first["task_id"], status="running", run_id=123)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (queue, "sha")
        dispatcher.store.save.return_value = "next-sha"
        dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
        dispatcher.runs = Mock(return_value=[{
            "id": 123, "status": "completed", "conclusion": "cancelled",
            "created_at": "2026-08-19T01:00:00Z", "updated_at": "2026-08-19T02:00:00Z",
            "display_title": f"有聲小說｜{first['task_id']}｜Ch1-100",
        }])
        dispatcher.progress_markers = Mock(return_value=None)
        dispatcher.dispatch_next = Mock(return_value=(queue, "started next"))

        result = dispatcher.run()
        self.assertIn("已啟動", result)
        dispatched_queue = dispatcher.dispatch_next.call_args.args[0]
        self.assertEqual(dispatched_queue["queue"][0]["status"], "interrupted")
        self.assertEqual(next_task(dispatched_queue)["task_id"], second["task_id"])

    def test_missing_bound_run_interrupts_book_and_starts_next(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, first["task_id"], status="running", run_id=404123)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (queue, "sha")
        dispatcher.store.save.return_value = "next-sha"
        dispatcher.runs = Mock(return_value=[])
        dispatcher.run_by_id = Mock(return_value=None)
        dispatcher.dispatch_next = Mock(return_value=(queue, "started next"))

        result = dispatcher.run()
        self.assertIn("已啟動", result)
        dispatched_queue = dispatcher.dispatch_next.call_args.args[0]
        interrupted = dispatched_queue["queue"][0]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["reason"], "run_not_found")
        self.assertEqual(interrupted["run_history"][0]["run_id"], 404123)
        self.assertEqual(next_task(dispatched_queue)["task_id"], second["task_id"])

    def test_mark_interrupted_is_idempotent_for_run_history(self):
        task = new_task("https://example/1", "第一部")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running", run_id=123)

        queue = mark_task_interrupted(queue, task["task_id"], ended_at="2026-08-19T02:00:00Z")
        queue = mark_task_interrupted(queue, task["task_id"], ended_at="2026-08-19T02:00:00Z")

        self.assertEqual(len(queue["queue"][0]["run_history"]), 1)

    def test_failed_run_needs_attention_and_can_be_requeued(self):
        task = new_task("https://example/1", "第一部")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running", run_id=456)

        queue = mark_task_needs_attention(
            queue, task["task_id"], reason="run_failure", conclusion="failure",
            ended_at="2026-08-20T02:00:00Z",
        )

        failed = queue["queue"][0]
        self.assertEqual(failed["status"], "waiting_retry")
        self.assertIsNotNone(failed.get("retry_at"))
        self.assertEqual(failed["run_history"][0]["run_id"], 456)
        queue = requeue_task_after_active(queue, task["task_id"])
        self.assertEqual(queue["queue"][0]["status"], "queued")
        self.assertIsNone(queue["queue"][0]["run_id"])

    def test_every_non_active_state_can_be_requeued(self):
        for status in ("queued", "paused", "stopped", "interrupted", "needs_attention"):
            with self.subTest(status=status):
                task = new_task("https://example/1", "第一部")
                queue = add_tasks(empty_queue(), [task])
                queue = update_task(queue, task["task_id"], status=status)

                queue = requeue_task_after_active(queue, task["task_id"])

                self.assertEqual(queue["queue"][0]["status"], "queued")

    def test_completed_task_moves_out_of_queue_and_releases_its_position(self):
        first = new_task("https://example/1", "吞噬星空")
        second = new_task("https://example/2", "修真聊天群")
        third = new_task("https://example/3", "完美世界")
        queue = add_tasks(empty_queue(), [first, second, third])

        queue = update_task(
            queue, second["task_id"], status="completed",
            completed_at="2026-08-22T08:56:35Z",
        )

        self.assertEqual([item["book_title"] for item in queue["queue"]], ["吞噬星空", "完美世界"])
        self.assertEqual([item["position"] for item in queue["queue"]], [1, 2])
        self.assertEqual([item["book_title"] for item in queue["completed"]], ["修真聊天群"])
        self.assertNotIn("position", queue["completed"][0])

    def test_dispatcher_success_moves_task_to_completed_in_same_update(self):
        finished = new_task("https://example/1", "修真聊天群")
        waiting = new_task("https://example/2", "完美世界")
        queue = add_tasks(empty_queue(), [finished, waiting])
        queue = update_task(queue, finished["task_id"], status="running", run_id=123)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[{
            "id": 123,
            "name": f"有聲小說製作｜修真聊天群｜Ch1-3300｜{finished['task_id']}",
            "status": "completed",
            "conclusion": "success",
            "updated_at": "2026-08-22T08:56:35Z",
        }])
        dispatcher.progress_markers = Mock(return_value=None)

        reconciled, changed = dispatcher.reconcile(queue)

        self.assertTrue(changed)
        self.assertEqual([item["book_title"] for item in reconciled["queue"]], ["完美世界"])
        self.assertEqual(reconciled["queue"][0]["position"], 1)
        self.assertEqual(reconciled["completed"][0]["book_title"], "修真聊天群")
        self.assertNotIn("position", reconciled["completed"][0])

    def test_requeued_book_ignores_runs_created_before_retry_request(self):
        task = new_task("https://example/1", "第一部")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"], status="queued", run_id=None,
            retry_requested_at="2026-08-19T03:00:00+00:00",
        )
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.runs = Mock(return_value=[{
            "id": 123, "status": "completed", "conclusion": "cancelled",
            "created_at": "2026-08-19T02:00:00Z", "updated_at": "2026-08-19T02:30:00Z",
            "display_title": f"有聲小說｜{task['task_id']}｜Ch1-100",
        }])

        queue, changed = dispatcher.reconcile(queue)

        self.assertFalse(changed)
        self.assertEqual(queue["queue"][0]["status"], "queued")
        self.assertIsNone(queue["queue"][0]["run_id"])

    @patch.object(Dispatcher, "request")
    def test_dispatcher_passes_book_title_to_workflow(self, request):
        task = new_task("https://example/1", "凡人修仙傳", 1, 100)
        task["run_history"] = [{
            "run_id": 122, "conclusion": "failure", "ended_at": "2026-08-22T00:00:00Z",
        }]
        queue = add_tasks(empty_queue(), [task])
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (queue, "sha")
        dispatcher.store.save.return_value = "next-sha"
        dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
        dispatcher.select_artifact_source_run_id = Mock(return_value=122)
        dispatcher.runs = Mock(side_effect=[
            [],
            [{
                "id": 123,
                "name": f"有聲小說製作｜凡人修仙傳｜Ch1-100｜{task['task_id']}",
                "status": "in_progress",
                "created_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
            }],
        ])

        dispatcher.dispatch_next(queue)

        dispatch_call = next(
            call for call in request.call_args_list
            if call.args[:2] == ("POST", "/actions/workflows/audiobook.yml/dispatches")
        )
        self.assertEqual(dispatch_call.kwargs["json"]["inputs"]["book_title"], "凡人修仙傳")
        self.assertEqual(dispatch_call.kwargs["json"]["inputs"]["chapter_label"], "Ch1-100")
        self.assertEqual(dispatch_call.kwargs["json"]["inputs"]["resume_source_run_id"], "122")
        reserved_queue = dispatcher.store.save.call_args_list[0].args[0]
        self.assertEqual(reserved_queue["queue"][0]["artifact_source_run_id"], 122)
        self.assertTrue(reserved_queue["queue"][0]["book_profile_id"])

    @patch.object(Dispatcher, "request")
    def test_dispatcher_does_not_bind_old_run_with_same_task_id(self, request):
        task = new_task("https://example/1", "凡人修仙傳", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (queue, "sha")
        dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
        old_run = {
            "id": 122,
            "name": f"有聲小說製作｜凡人修仙傳｜Ch1-100｜{task['task_id']}",
            "status": "completed",
            "created_at": "2026-08-21T00:00:00+00:00",
        }
        new_run = {
            **old_run,
            "id": 123,
            "status": "in_progress",
            "created_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
        }
        dispatcher.runs = Mock(side_effect=[[], [old_run, new_run]])

        dispatcher.dispatch_next(queue)

        attached = dispatcher.store.save.call_args_list[-1].args[0]
        self.assertEqual(attached["queue"][0]["run_id"], 123)

    def test_format_chapter_label(self):
        # 1. No exclusion
        self.assertEqual(format_chapter_label(1, 100), "Ch1-100")
        # 2. Excluded without renumber
        self.assertEqual(format_chapter_label(1, 3300, excluded_chapters=list(range(1, 145))), "Ch1-3300 (實做3156章)")
        # 3. Excluded with renumber
        self.assertEqual(format_chapter_label(1, 3300, excluded_chapters=list(range(1, 145)), renumber_selected=True), "Ch1-3156 (共3156章)")
        # 4. Partial range without renumber
        self.assertEqual(format_chapter_label(101, 200, excluded_chapters=[150, 151]), "Ch101-200 (實做98章)")
        # 5. Partial range with renumber
        self.assertEqual(format_chapter_label(101, 200, excluded_chapters=[150, 151], renumber_selected=True), "Ch1-98 (共98章)")
        # 6. Unbounded end
        self.assertEqual(format_chapter_label(1, 999999), "Ch1-全部")

    def test_active_task_is_always_position_1(self):
        first = new_task("https://example/1", "吞噬星空")
        second = new_task("https://example/2", "修真聊天群")
        queue = add_tasks(empty_queue(), [first, second])
        self.assertEqual(queue["queue"][0]["book_title"], "吞噬星空")
        self.assertEqual(queue["queue"][1]["book_title"], "修真聊天群")

        # When second starts running, it must automatically become position 1
        queue = update_task(queue, second["task_id"], status="running", run_id=32326794640)
        self.assertEqual(queue["queue"][0]["book_title"], "修真聊天群")
        self.assertEqual(queue["queue"][0]["position"], 1)
        self.assertEqual(queue["queue"][1]["book_title"], "吞噬星空")
        self.assertEqual(queue["queue"][1]["position"], 2)

    def test_requeue_task_places_behind_currently_running_task(self):
        task1 = new_task("https://example/1", "吞噬星空")
        task2 = new_task("https://example/2", "修真聊天群")
        task3 = new_task("https://example/3", "完美世界")
        queue = add_tasks(empty_queue(), [task1, task2, task3])

        # task1 was interrupted, task2 is running
        queue = update_task(queue, task1["task_id"], status="interrupted")
        queue = update_task(queue, task2["task_id"], status="running", run_id=32326794640)

        self.assertEqual(queue["queue"][0]["book_title"], "修真聊天群")
        self.assertEqual(queue["queue"][0]["position"], 1)

        # Requeue task1 -> must be position 2 (behind task2), task3 is position 3
        queue = requeue_task_after_active(queue, task1["task_id"])
        self.assertEqual(queue["queue"][0]["book_title"], "修真聊天群")
        self.assertEqual(queue["queue"][0]["position"], 1)
        self.assertEqual(queue["queue"][1]["book_title"], "吞噬星空")
        self.assertEqual(queue["queue"][1]["position"], 2)
        self.assertEqual(queue["queue"][2]["book_title"], "完美世界")
        self.assertEqual(queue["queue"][2]["position"], 3)

    def test_requeue_task_with_explicit_active_id(self):
        task1 = new_task("https://example/1", "吞噬星空")
        task2 = new_task("https://example/2", "修真聊天群")
        queue = add_tasks(empty_queue(), [task1, task2])

        # Even if task2 status in json is still queued but GUI knows it's active
        queue = requeue_task_after_active(queue, task1["task_id"], active_id=task2["task_id"])
        self.assertEqual(queue["queue"][0]["book_title"], "修真聊天群")
        self.assertEqual(queue["queue"][0]["position"], 1)
        self.assertEqual(queue["queue"][1]["book_title"], "吞噬星空")
        self.assertEqual(queue["queue"][1]["position"], 2)


    @patch.object(Dispatcher, "request")
    def test_force_dispatch_ignores_retry_at_without_claiming_unobserved_run(self, request):
        task = new_task("https://example/1", "修真聊天群")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"],
            status="waiting_retry",
            retry_at="2099-01-01T00:00:00+00:00",
            reason="run_failure",
        )
        dispatcher = Dispatcher("owner/repo", "token", force=True)
        dispatcher.store.load = Mock(return_value=(queue, "sha-1"))
        dispatcher.store.save = Mock(return_value="sha-2")
        dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
        dispatcher.runs = Mock(return_value=[])

        summary = dispatcher.run()
        request.assert_called_once()
        self.assertNotIn("已啟動", summary)
        self.assertIn("沒有可啟動的任務", summary)

    @patch.object(Dispatcher, "dispatch_next")
    def test_retry_on_stale_commit_dispatches_fresh_run(self, dispatch_next):
        task = new_task("https://example/1", "修真聊天群")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(
            queue, task["task_id"], status="waiting_retry", run_id=123,
            retry_at="2020-01-01T00:00:00+00:00",
        )
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store.load = Mock(return_value=(queue, "sha-1"))
        dispatcher.store.save = Mock(return_value="sha-2")
        dispatcher.profile_store.load = Mock(return_value=({"books": {}}, None))
        dispatcher.reconcile = Mock(return_value=(queue, False))
        dispatcher.run_uses_current_master = Mock(return_value=False)
        dispatch_next.side_effect = lambda value: (value, "fresh")

        dispatcher.run()

        dispatcher.run_uses_current_master.assert_called_once_with(123)
        dispatched_queue = dispatch_next.call_args.args[0]
        self.assertEqual(dispatched_queue["queue"][0]["status"], "queued")

    @patch.object(Dispatcher, "dispatch_next")
    def test_unobserved_dispatch_is_not_reported_as_launched(self, dispatch_next):
        task = new_task("https://example/1", "修真聊天群")
        queue = add_tasks(empty_queue(), [task])
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store.load = Mock(return_value=(queue, "sha-1"))
        dispatcher.reconcile = Mock(return_value=(queue, False))
        dispatch_next.side_effect = lambda value: (value, "not observed")

        summary = dispatcher.run()

        self.assertNotIn("已啟動《", summary)


if __name__ == "__main__":
    unittest.main()
