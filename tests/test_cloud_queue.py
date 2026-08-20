import unittest
from unittest.mock import Mock, patch

from src.cloud_queue import (
    add_tasks, current_task, empty_queue, move_task, move_tasks, new_task, next_task,
    mark_task_interrupted, mark_task_needs_attention, requeue_task_after_active, update_task,
    update_task_chapters,
)
from src.queue_dispatcher import Dispatcher


class CloudQueueTests(unittest.TestCase):
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
        self.assertEqual(queue["tasks"][0]["duplicate_chapter_count"], 9)

    def test_chapter_plan_update_normalizes_exclusions(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])

        queue = update_task_chapters(queue, task["task_id"], 10, 20, [1, 10, 12, 12, 30])

        edited = queue["tasks"][0]
        self.assertEqual((edited["start_chapter"], edited["end_chapter"]), (10, 20))
        self.assertEqual(edited["excluded_chapters"], [10, 12])

    def test_chapter_plan_persists_selected_chapter_renumbering(self):
        task = new_task("https://example/1", "第一部", 1, 5)
        queue = add_tasks(empty_queue(), [task])

        queue = update_task_chapters(
            queue, task["task_id"], 1, 5, [2, 4], renumber_selected=True,
        )

        edited = queue["tasks"][0]
        self.assertEqual(edited["excluded_chapters"], [2, 4])
        self.assertTrue(edited["renumber_selected"])

    def test_active_chapter_plan_update_waits_for_cancel_before_requeue(self):
        task = new_task("https://example/1", "第一部", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running", run_id=123)

        queue = update_task_chapters(queue, task["task_id"], 20, 80, [], requeue_after_cancel=True)

        edited = queue["tasks"][0]
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

    def test_paused_task_is_skipped_and_can_be_reordered(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, first["task_id"], status="paused")
        self.assertEqual(next_task(queue)["task_id"], second["task_id"])
        queue = move_task(queue, first["task_id"], 2)
        self.assertEqual(queue["tasks"][1]["task_id"], first["task_id"])

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
        self.assertEqual([item["book_title"] for item in queue["tasks"]], [
            "第2部", "第1部", "第4部", "第3部", "第5部",
        ])

        queue = move_tasks(queue, selected, 1)
        self.assertEqual([item["book_title"] for item in queue["tasks"]], [
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

        self.assertEqual([item["task_id"] for item in queue["tasks"]], [
            active["task_id"], interrupted["task_id"], waiting["task_id"],
        ])
        retried = queue["tasks"][1]
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
        self.assertEqual(queue["tasks"][0]["status"], "interrupted")
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
        edited = queue["tasks"][0]
        self.assertEqual(edited["status"], "queued")
        self.assertFalse(edited["requeue_after_edit"])
        self.assertIsNone(edited["run_id"])

    def test_dispatcher_starts_next_book_after_cancelled_run(self):
        first = new_task("https://example/1", "第一部")
        second = new_task("https://example/2", "第二部")
        queue = add_tasks(empty_queue(), [first, second])
        queue = update_task(queue, first["task_id"], status="running", run_id=123)
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (queue, "sha")
        dispatcher.store.save.return_value = "next-sha"
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
        self.assertEqual(dispatched_queue["tasks"][0]["status"], "interrupted")
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
        interrupted = dispatched_queue["tasks"][0]
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

        self.assertEqual(len(queue["tasks"][0]["run_history"]), 1)

    def test_failed_run_needs_attention_and_can_be_requeued(self):
        task = new_task("https://example/1", "第一部")
        queue = add_tasks(empty_queue(), [task])
        queue = update_task(queue, task["task_id"], status="running", run_id=456)

        queue = mark_task_needs_attention(
            queue, task["task_id"], reason="run_failure", conclusion="failure",
            ended_at="2026-08-20T02:00:00Z",
        )

        failed = queue["tasks"][0]
        self.assertEqual(failed["status"], "needs_attention")
        self.assertEqual(failed["run_history"][0]["run_id"], 456)
        queue = requeue_task_after_active(queue, task["task_id"])
        self.assertEqual(queue["tasks"][0]["status"], "queued")
        self.assertIsNone(queue["tasks"][0]["run_id"])

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
        self.assertEqual(queue["tasks"][0]["status"], "queued")
        self.assertIsNone(queue["tasks"][0]["run_id"])

    @patch.object(Dispatcher, "request")
    def test_dispatcher_passes_book_title_to_workflow(self, request):
        task = new_task("https://example/1", "凡人修仙傳", 1, 100)
        queue = add_tasks(empty_queue(), [task])
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (queue, "sha")
        dispatcher.store.save.return_value = "next-sha"
        dispatcher.runs = Mock(return_value=[{
            "id": 123,
            "name": f"有聲小說製作｜凡人修仙傳｜Ch1-100｜{task['task_id']}",
        }])

        dispatcher.dispatch_next(queue)

        dispatch_call = next(
            call for call in request.call_args_list
            if call.args[:2] == ("POST", "/actions/workflows/audiobook.yml/dispatches")
        )
        self.assertEqual(dispatch_call.kwargs["json"]["inputs"]["book_title"], "凡人修仙傳")


if __name__ == "__main__":
    unittest.main()
