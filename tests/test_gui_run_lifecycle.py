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
