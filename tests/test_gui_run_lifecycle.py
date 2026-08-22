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

    assert 'text="完成"' in dialog_source
    assert 'text="取消"' not in dialog_source
    assert "套用到預覽（尚未儲存）" not in dialog_source
    assert 'dialog.protocol("WM_DELETE_WINDOW", finish_editing)' in dialog_source
    assert "dialog.grab_set()" not in dialog_source
    assert "儲存到 GitHub" not in dialog_source
    assert "._profile_store(" not in dialog_source
    assert "on_applied(self.cleaner_remove_patterns)" in dialog_source
    assert "scrolledtext.ScrolledText" in dialog_source
    assert 'entry.get("1.0", "end-1c")' in dialog_source
    assert "特殊符號視為普通文字" in dialog_source


def test_text_preview_uses_current_draft_and_ignores_stale_refreshes():
    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"))
    preview = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AudiobookGUIApp"
        for node in node.body
        if isinstance(node, ast.FunctionDef) and node.name == "open_text_sample"
    )
    preview_source = ast.get_source_segment(GUI_SOURCE.read_text(encoding="utf-8"), preview)

    assert "refresh_preview(self.cleaner_remove_patterns)" in preview_source
    assert 'generation != preview_generation["value"]' in preview_source
    assert 'profile.get("cleaner_remove_patterns")' not in preview_source
    normal = preview_source.index("box.config(state=tk.NORMAL)")
    delete = preview_source.index('box.delete("1.0", tk.END)')
    insert = preview_source.index('box.insert("1.0", content)')
    disabled = preview_source.index("box.config(state=tk.DISABLED)", insert)
    assert normal < delete < insert < disabled


def test_chapter_update_persists_the_cleaner_pattern_snapshot():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert "cleaner_patterns = validate_remove_patterns(self.cleaner_remove_patterns)" in source
    assert "cleaner_remove_patterns=cleaner_patterns" in source


def test_chapter_order_shortcuts_return_tk_break_directly():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert 'return "break"' in source
    assert 'lambda _event: (_move_selected(-1), "break")' not in source
    assert 'lambda _event: (_move_selected(1), "break")' not in source


def test_batch_add_uses_the_same_explicit_order_and_numbering_as_single_add():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    batch_start = source.index("def open_batch_queue_dialog")
    batch_end = source.index("def move_selected_task", batch_start)
    batch_source = source[batch_start:batch_end]

    assert "renumber_selected=True" in batch_source
    assert 'chapter_order=list(range(1, result["total_chapters"] + 1))' in batch_source


def test_partial_chapter_update_reports_completed_cloud_writes():
    source = GUI_SOURCE.read_text(encoding="utf-8")

    assert 'completed_steps.append("書籍清理設定")' in source
    assert 'completed_steps.append("章節範圍與順序")' in source
    assert "已成功寫入" in source
