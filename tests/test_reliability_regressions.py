import asyncio
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.cloud_queue import QueueConflict, add_tasks, empty_queue, new_task
from src.queue_dispatcher import Dispatcher
from src.tts_ms import segment_cache_key


class ReliabilityTests(unittest.TestCase):
    def test_reservation_rejects_stale_queue_before_dispatch(self):
        before = add_tasks(empty_queue(), [new_task("https://example.com/1", "A")])
        after = add_tasks(before, [new_task("https://example.com/2", "B")])
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.store = Mock()
        dispatcher.store.load.return_value = (after, "new-sha")
        dispatcher.request = Mock()
        with self.assertRaises(QueueConflict):
            dispatcher.dispatch_next(before)
        dispatcher.store.save.assert_not_called()
        dispatcher.request.assert_not_called()

    def test_cache_distinguishes_paragraph_pause(self):
        self.assertNotEqual(
            segment_cache_key("測試。", "zh-CN-YunjianNeural", "+25%", paragraph_end=False),
            segment_cache_key("測試。", "zh-CN-YunjianNeural", "+25%", paragraph_end=True),
        )
