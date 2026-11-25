"""Open the real chapter selection dialog with all nine-site-independent state.

The form is exercised as Tk widgets, rather than only by reading its source.
"""
import tkinter as tk
from tkinter import ttk
from unittest.mock import Mock, patch

from gui_app import AudiobookGUIApp


def _widgets(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from _widgets(widget)


def test_chapter_editor_can_open_sort_and_close_without_a_network_request():
    root = tk.Tk()
    root.withdraw()
    app = object.__new__(AudiobookGUIApp)
    app.root = root
    app.catalog_data = {
        "book_title": "測試書", "source_id": "czbooks",
        "catalog_url": "https://czbooks.net/n/c37492",
        "chapter_titles": ["第3章 結尾", "第1章 開始", "第2章 中段"],
        "chapters": [
            "https://czbooks.net/n/c37492/c1?chapterNumber=1",
            "https://czbooks.net/n/c37492/c2?chapterNumber=2",
            "https://czbooks.net/n/c37492/c3?chapterNumber=3",
        ],
    }
    app.excluded_chapters = set()
    app.chapter_order = [3, 1, 2]
    app.chapter_normalized_number_overrides = {}
    app.chapter_title_overrides = {}
    app.duplicate_detection = {
        "use_normalized_number": True,
        "use_chapter_name": True,
        "use_number_and_name": False,
    }
    app.renumber_selected_chapters = False
    app.cleaner_remove_patterns = []
    app.url_entry = ttk.Entry(root)
    app.url_entry.insert(0, "https://czbooks.net/n/c37492")
    app.entry_start = ttk.Entry(root)
    app.entry_start.insert(0, "1")
    app.entry_end = ttk.Entry(root)
    app.entry_end.insert(0, "3")
    app._update_chapter_selection_summary = Mock()
    app._selected_task = Mock(return_value=None)
    try:
        with patch("gui_components.chapter_filter_dialog.threading.Thread") as thread:
            app._open_chapter_filter_dialog()
            dialogs = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
            assert len(dialogs) == 1
            dialog = dialogs[0]
            tree = next(w for w in _widgets(dialog) if isinstance(w, ttk.Treeview))
            assert len(tree.get_children()) == 3
            sorting = next(w for w in _widgets(dialog)
                           if isinstance(w, ttk.Button) and
                           "依網站章節正規化順序排列" in w.cget("text"))
            sorting.invoke()
            assert app.renumber_selected_chapters is True
            confirm = next(w for w in _widgets(dialog)
                           if isinstance(w, ttk.Button) and w.cget("text") == "確定")
            confirm.invoke()
            assert app.chapter_order == [2, 3, 1]
            assert app._update_chapter_selection_summary.call_count == 1
            assert thread.call_count == 1
    finally:
        root.destroy()
