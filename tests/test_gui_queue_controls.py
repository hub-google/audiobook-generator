import tkinter as tk
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from gui_app import AudiobookGUIApp


def make_app():
    app = object.__new__(AudiobookGUIApp)
    app.btn_toggle_task = Mock()
    app.btn_stop_task = Mock()
    app.btn_toggle_completed = Mock()
    app.btn_sample_text = Mock()
    app.github_observations = {}
    return app


class GuiQueueControlTests(unittest.TestCase):
    @patch("gui_app.requests.get")
    def test_missing_preflight_run_is_recovered_by_task_and_dispatch_time(self, get):
        response = Mock()
        response.json.return_value = {"workflow_runs": [
            {
                "id": 90,
                "display_title": "【TXT抓取】old｜book-20260907-046571a3",
                "created_at": "2026-09-07T10:00:00Z",
            },
            {
                "id": 34115699942,
                "display_title": "【TXT抓取】current｜book-20260907-046571a3",
                "created_at": "2026-09-07T11:15:32Z",
            },
            {
                "id": 999,
                "display_title": "【TXT抓取】another｜book-20260907-deadbeef",
                "created_at": "2026-09-07T12:00:00Z",
            },
        ]}
        get.return_value = response

        run = AudiobookGUIApp._discover_preflight_run(
            "owner/repo", "token", "audiobook.yml", "book-20260907-046571a3",
            "2026-09-07T19:15:29+08:00",
        )

        self.assertEqual(run["id"], 34115699942)
        response.raise_for_status.assert_called_once()

    def test_preflight_observations_unlock_review_only_after_both_artifacts(self):
        queue = {"queue": [{
            "task_id": "book-1", "workflow_phase": "preflight", "status": "preparing_assets",
            "scrape_run_id": 100, "cover_run_id": 200,
            "stages": {
                "scrape": {"run_id": 100, "status": "running"},
                "cover": {"run_id": 200, "status": "running"},
            },
        }]}

        changed = AudiobookGUIApp._apply_preflight_observations(queue, {
            "book-1": {
                "scrape": {"run_id": 100, "status": "completed", "updated_at": "2026-09-07T11:33:49Z"},
            },
        })
        self.assertTrue(changed)
        self.assertEqual(queue["queue"][0]["status"], "preparing_assets")
        self.assertEqual(queue["queue"][0]["stages"]["scrape"]["status"], "completed")

        AudiobookGUIApp._apply_preflight_observations(queue, {
            "book-1": {
                "cover": {"run_id": 200, "status": "completed", "updated_at": "2026-09-07T10:02:00Z"},
            },
        })
        self.assertEqual(queue["queue"][0]["status"], "waiting_review")

    def test_preflight_observation_cannot_overwrite_a_newer_bound_run(self):
        queue = {"queue": [{
            "task_id": "book-1", "workflow_phase": "preflight", "status": "preparing_assets",
            "scrape_run_id": 101, "cover_run_id": 200,
            "stages": {
                "scrape": {"run_id": 101, "status": "running"},
                "cover": {"run_id": 200, "status": "running"},
            },
        }]}

        AudiobookGUIApp._apply_preflight_observations(queue, {
            "book-1": {"scrape": {"run_id": 100, "status": "completed"}},
        })

        self.assertEqual(queue["queue"][0]["scrape_run_id"], 101)
        self.assertEqual(queue["queue"][0]["stages"]["scrape"]["status"], "running")

    def test_queue_buttons_only_enable_for_supported_states(self):
        app = make_app()

        app._update_queue_control_states({"status": "queued"})
        self.assertEqual(app.btn_toggle_task.config.call_args.kwargs["state"], tk.NORMAL)
        self.assertEqual(app.btn_stop_task.config.call_args.kwargs["state"], tk.DISABLED)
        self.assertEqual(app.btn_toggle_completed.config.call_args.kwargs["state"], tk.NORMAL)
        self.assertEqual(app.btn_toggle_completed.config.call_args.kwargs["text"], "標記為已完成")

        app._update_queue_control_states({"status": "completed"})
        self.assertEqual(app.btn_toggle_completed.config.call_args.kwargs["state"], tk.NORMAL)
        self.assertEqual(app.btn_toggle_completed.config.call_args.kwargs["text"], "移回未完成")

        app._update_queue_control_states([])
        self.assertEqual(app.btn_toggle_completed.config.call_args.kwargs["state"], tk.DISABLED)

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
        app._update_queue_control_states({
            "status": "waiting_review", "scrape_run_id": 123,
            "stages": {"scrape": {"status": "completed"}},
        })
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

    def test_queued_task_without_run_displays_idle(self):
        app = make_app()
        task = {"task_id": "task-1", "run_id": None, "status": "queued"}
        self.assertEqual(app._queue_status_text(task), "① 等待啟動")


if __name__ == "__main__":
    unittest.main()
