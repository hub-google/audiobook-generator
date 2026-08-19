from pathlib import Path


GUI_SOURCE = Path(__file__).resolve().parents[1] / "gui_app.py"


def test_gui_does_not_resume_a_run_from_a_previous_process():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert "_resume_saved_run" not in source
    assert "ACTIVE_RUN_PATH" not in source
    assert "_save_active_run" not in source


def test_network_recovery_remains_in_the_current_run_monitor():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert "while True:" in source
    assert "網路已恢復，正在重新同步雲端 Run、Jobs 與執行紀錄" in source
    assert "GUI 將持續重試" in source


def test_queue_sync_checks_bound_run_against_github():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert "actions/runs/{run_id}" in source
    assert "run_not_found" in source
    assert "mark_task_interrupted" in source
    assert "self.root.after(10000, self.sync_cloud_queue)" in source
    assert "observation_text" in source
    assert "GitHub 查證" in source
    assert "_refresh_observation_freshness" in source
    assert "self.root.after(1000, self._refresh_observation_freshness)" in source
