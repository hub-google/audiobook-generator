import ast
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


def test_deferred_gui_callbacks_do_not_capture_exception_targets():
    """Exception targets are cleared when an except block exits (PEP 3110)."""
    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"))
    unsafe = []
    for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
        if not handler.name:
            continue
        for child in ast.walk(handler):
            if not isinstance(child, ast.Lambda):
                continue
            bound = {arg.arg for arg in child.args.args}
            referenced = {
                node.id for node in ast.walk(child.body) if isinstance(node, ast.Name)
            }
            if handler.name in referenced and handler.name not in bound:
                unsafe.append((child.lineno, handler.name))

    assert unsafe == []


def test_run_discovery_callback_binds_the_discovered_run_id():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert "lambda rid=run_id, s=status" in source
    assert "Run ID #{rid}" in source


def test_cleaner_pattern_dialog_only_applies_a_local_draft():
    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"))
    dialog = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AudiobookGUIApp"
        for node in node.body
        if isinstance(node, ast.FunctionDef) and node.name == "_open_cleaner_patterns_dialog"
    )
    dialog_source = ast.get_source_segment(GUI_SOURCE.read_text(encoding="utf-8"), dialog)

    assert "套用到預覽（尚未儲存）" in dialog_source
    assert "儲存到 GitHub" not in dialog_source
    assert "._profile_store(" not in dialog_source
    assert "on_applied(self.cleaner_remove_patterns)" in dialog_source


def test_chapter_update_persists_the_cleaner_pattern_snapshot():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert "cleaner_patterns = validate_remove_patterns(self.cleaner_remove_patterns)" in source
    assert "cleaner_remove_patterns=cleaner_patterns" in source
