import unittest
from unittest.mock import Mock, patch

from src.cloud_queue import add_tasks, current_task, empty_queue, move_task, new_task, next_task, update_task
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
