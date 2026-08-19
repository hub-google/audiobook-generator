import tkinter as tk
import unittest
from unittest.mock import Mock

from gui_app import AudiobookGUIApp


def make_app():
    app = object.__new__(AudiobookGUIApp)
    app.btn_toggle_task = Mock()
    app.btn_stop_task = Mock()
    app.github_observations = {}
    return app


class GuiQueueControlTests(unittest.TestCase):
    def test_queue_buttons_only_enable_for_supported_states(self):
        app = make_app()

        app._update_queue_control_states({"status": "queued"})
        self.assertEqual(app.btn_toggle_task.config.call_args.kwargs["state"], tk.NORMAL)
        self.assertEqual(app.btn_stop_task.config.call_args.kwargs["state"], tk.DISABLED)

        app._update_queue_control_states({"status": "running"})
        self.assertEqual(app.btn_toggle_task.config.call_args.kwargs["state"], tk.DISABLED)
        self.assertEqual(app.btn_stop_task.config.call_args.kwargs["state"], tk.NORMAL)

        app._update_queue_control_states({"status": "canceling"})
        self.assertEqual(app.btn_stop_task.config.call_args.kwargs, {
            "state": tk.DISABLED,
            "text": "正在取消…",
        })

    def test_canceling_status_is_not_hidden_by_stale_github_observation(self):
        app = make_app()
        task = {"task_id": "task-1", "run_id": 123, "status": "canceling"}

        self.assertEqual(app._queue_status_text(task), "正在取消 Run")


if __name__ == "__main__":
    unittest.main()
