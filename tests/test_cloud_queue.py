import unittest

from src.cloud_queue import add_tasks, current_task, empty_queue, move_task, new_task, next_task, update_task


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


if __name__ == "__main__":
    unittest.main()
