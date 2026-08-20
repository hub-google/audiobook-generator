import tkinter as tk
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from gui_app import AudiobookGUIApp


def make_app():
    app = object.__new__(AudiobookGUIApp)
    app.btn_toggle_task = Mock()
    app.btn_stop_task = Mock()
    app.btn_sample_text = Mock()
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

    def test_text_sample_only_enables_for_single_selection(self):
        app = make_app()
        app._update_queue_control_states([{"status": "queued"}, {"status": "paused"}])
        self.assertEqual(app.btn_sample_text.config.call_args.kwargs["state"], tk.DISABLED)
        app._update_queue_control_states({"status": "completed"})
        self.assertEqual(app.btn_sample_text.config.call_args.kwargs["state"], tk.NORMAL)

    def test_sample_positions_use_filtered_lower_middle_and_output_number(self):
        catalog = {
            "total_chapters": 8,
            "base_url": "https://example.test",
            "chapters": [f"/read/{i}" for i in range(1, 9)],
            "chapter_titles": [f"第{i}章" for i in range(1, 9)],
        }
        task = {
            "start_chapter": 1, "end_chapter": 8,
            "excluded_chapters": [2, 4], "renumber_selected": True,
        }
        samples = AudiobookGUIApp._text_sample_chapters(task, catalog)
        self.assertEqual([item["source_index"] for item in samples], [1, 5, 8])
        self.assertEqual([item["output_index"] for item in samples], [1, 3, 6])

    def test_2000_chapter_middle_is_1000(self):
        catalog = {
            "total_chapters": 2000, "base_url": "https://example.test",
            "chapters": [f"/{i}" for i in range(1, 2001)],
            "chapter_titles": [str(i) for i in range(1, 2001)],
        }
        samples = AudiobookGUIApp._text_sample_chapters(
            {"start_chapter": 1, "end_chapter": 2000}, catalog,
        )
        self.assertEqual(samples[1]["source_index"], 1000)

    def test_canceling_status_is_not_hidden_by_stale_github_observation(self):
        app = make_app()
        task = {"task_id": "task-1", "run_id": 123, "status": "canceling"}

        self.assertEqual(app._queue_status_text(task), "canceling")

    def test_verified_github_run_state_overrides_local_queue_state(self):
        app = make_app()
        task = {"task_id": "task-1", "run_id": 123, "status": "needs_attention"}
        app.github_observations["task-1"] = {
            "kind": "ok", "raw_status": "in_progress", "raw_conclusion": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        self.assertEqual(app._queue_status_text(task), "in_progress")

    def test_stop_task_enables_when_task_has_run_id(self):
        app = make_app()
        app._update_queue_control_states({"status": "interrupted", "run_id": 32323730742})
        self.assertEqual(app.btn_stop_task.config.call_args.kwargs["state"], tk.NORMAL)

        app._update_queue_control_states({"status": "needs_attention", "run_id": 32323730742})
        self.assertEqual(app.btn_stop_task.config.call_args.kwargs["state"], tk.NORMAL)


if __name__ == "__main__":
    unittest.main()
