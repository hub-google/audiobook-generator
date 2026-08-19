import unittest
from unittest.mock import Mock, patch

from src.cloud_queue import (
    add_tasks, current_task, empty_queue, move_task, new_task, next_task,
    mark_task_interrupted, requeue_task_after_active, update_task,
)
from src.queue_dispatcher import Dispatcher


class CloudQueueTests(unittest.TestCase):
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

        self.assertEqual(dispatcher.run(), "started next")
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

        self.assertEqual(dispatcher.run(), "started next")
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
