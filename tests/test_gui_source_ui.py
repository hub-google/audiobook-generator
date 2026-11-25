"""No network: nine-site GUI catalog routing, safety and batch handling."""
import tkinter as tk
from tkinter import ttk
from unittest.mock import Mock, patch

import pytest

from gui_app import AudiobookGUIApp
from gui_components.source_ui import (
    AUTO_SOURCE_LABEL, SOURCE_EXAMPLES, SOURCE_NAMES, SOURCE_OPTIONS,
    catalog_matches_current, detect_source, normalize_batch_urls,
    normalize_source_url, source_hint, source_id_from_label, source_label,
)
from src.sources.base import SourceParseError
from src.sources.registry import SOURCES


@pytest.mark.parametrize("source_id", list(SOURCE_NAMES))
def test_source_selector_is_backed_by_a_live_registered_adapter(source_id):
    assert len(SOURCE_NAMES) == len(SOURCES) == 9
    raw = SOURCE_EXAMPLES[source_id]
    adapter, canonical = normalize_source_url(raw)
    assert adapter.source_id == source_id
    assert adapter.matches(canonical)
    assert adapter.book_id(raw) == adapter.book_id(canonical)
    assert source_id_from_label(source_label(source_id)) == source_id


@pytest.mark.parametrize("url,source_id", [
    ("https://www.69shuba.com/book/53782.htm", "shuba69"),
    ("https://69shuba.com/txt/53782/34821656", "shuba69"),
    ("https://czbooks.net/n/c37492/cnj7?chapterNumber=0", "czbooks"),
    ("https://www.twkan.cc/chapter/5xqpGOcl46z9RzbASUJWgx/123.html", "twkan"),
    ("https://xszj.tw/book/491274/chapter-123", "xszj"),
    ("https://m.metanovel.org/book/480384/chapter/57083", "metanovel"),
    ("https://www.17moon.com/book/8Kk/Fv2Bq.html", "moon17"),
    ("https://8book.com/read/168909/?39876", "book8"),
    ("https://tw.hjwzw.com/Book/Read/35120,1", "hjwzw"),
    ("https://www.6ksu.app/novel/5050292/chapter/63633053.html", "sixk"),
])
def test_chapter_or_book_page_goes_to_source_specific_catalog(url, source_id):
    adapter, catalog = normalize_source_url(url)
    assert adapter.source_id == source_id
    assert adapter.book_id(catalog) == adapter.book_id(url)


@pytest.mark.parametrize("bad", [
    "", "hello", "https://evil.com/book/1",
    "https://www.69shuba.com.evil.com/book/53782/",
    "file:///etc/passwd", "https://user:pw@czbooks.net/n/c37492",
    "https://czbooks.net/n/not-allowed/read/1",
    "https://[",
])
def test_invalid_urls_never_go_to_network(bad):
    with pytest.raises(SourceParseError):
        normalize_source_url(bad)


def test_batch_mixes_sites_and_deduplicates_by_book_identity():
    urls = [
        "https://www.69shuba.com/book/53782.htm",
        "https://69shuba.com/txt/53782/34821656",
        "https://czbooks.net/n/c37492",
        "https://czbooks.net/n/c37492/cnj7?chapterNumber=0",
        "https://www.17moon.com/book/8Kk/Fv2Bq.html",
        "https://8book.com/read/168909/?39876",
    ]
    result = normalize_batch_urls(urls)
    assert len(result) == 4
    assert result[0] == "https://69shuba.com/book/53782/"
    assert result[1] == "https://czbooks.net/n/c37492"
    assert result[2] == "https://www.17moon.com/book/8Kk/"
    assert result[3] == "https://8book.com/novelbooks/168909/"


def test_stale_catalog_does_not_allow_a_new_book_to_be_queued():
    adapter, canonical = normalize_source_url("https://czbooks.net/n/c37492")
    parsed = {"success": True, "source_id": adapter.source_id,
              "catalog_url": canonical, "total_chapters": 119}
    assert catalog_matches_current(canonical, parsed)
    assert catalog_matches_current("https://czbooks.net/n/c37492/cnj7?chapterNumber=0", parsed)
    assert not catalog_matches_current("https://czbooks.net/n/not_same", parsed)
    assert not catalog_matches_current("https://8book.com/novelbooks/168909/", parsed)


def test_gui_selector_fills_example_but_preserves_custom_book_url():
    app = object.__new__(AudiobookGUIApp)
    root = tk.Tk()
    root.withdraw()
    try:
        app.root = root
        app.url_entry = tk.Entry(root)
        app.source_choice_var = tk.StringVar(value=SOURCE_NAMES["moon17"])
        app.source_hint_var = tk.StringVar()
        app.btn_add_queue = Mock()
        app.catalog_data = None
        app.editing_task_id = None
        app._on_source_selected()
        assert app.url_entry.get() == SOURCE_EXAMPLES["moon17"]
        app.source_choice_var.set(SOURCE_NAMES["czbooks"])
        app._on_source_selected()
        assert app.url_entry.get() == SOURCE_EXAMPLES["czbooks"]

        app.url_entry.delete(0, tk.END)
        app.url_entry.insert(0, "https://czbooks.net/n/my_custom_book")
        app.source_choice_var.set(SOURCE_NAMES["moon17"])
        app._on_source_selected()
        assert app.url_entry.get() == "https://czbooks.net/n/my_custom_book"
        assert "保留" in app.source_hint_var.get()
    finally:
        root.destroy()


def test_gui_enqueuing_rejects_stale_or_wrong_chapter_range():
    app = object.__new__(AudiobookGUIApp)
    app.catalog_data = {"success": True, "catalog_url": "https://czbooks.net/n/c37492",
                        "source_id": "czbooks", "total_chapters": 119}
    app.url_entry = Mock()
    app.url_entry.get.return_value = "https://8book.com/novelbooks/168909/"
    app.entry_start = Mock()
    app.entry_start.get.return_value = "1"
    app.entry_end = Mock()
    app.entry_end.get.return_value = "20"
    with patch("gui_components.queue_operations.messagebox.showwarning") as warning:
        app.enqueue_current_task()
    warning.assert_called_once()


def test_gui_preview_uses_actual_registered_chapter_parser():
    app = object.__new__(AudiobookGUIApp)
    app.catalog_data = {
        "success": True, "catalog_url": "https://8book.com/novelbooks/168909/",
        "source_id": "book8", "book_title": "紅樓夢",
        "chapters": ["https://8book.com/read/168909/?39876"],
    }
    app.url_entry = Mock()
    app.url_entry.get.return_value = app.catalog_data["catalog_url"]
    app.btn_preview_chapter = Mock()
    app.log = Mock()
    app.root = Mock()
    # Run the worker synchronously; Tk callbacks are still tested as callbacks.
    def immediate_thread(target, daemon=True):
        thread = Mock()
        thread.start.side_effect = target
        return thread
    with patch("gui_components.chapter_editor.threading.Thread", side_effect=immediate_thread):
        with patch("gui_components.chapter_editor.fetch_chapter_text", return_value=("第1章", "正文內容")) as fetch:
            app.preview_first_chapter()
    fetch.assert_called_once_with("https://8book.com/read/168909/?39876", source_id="book8")
    assert app.root.after.call_count >= 2


def test_gui_invalid_url_reports_error_before_starting_network():
    app = object.__new__(AudiobookGUIApp)
    app.url_entry = Mock()
    app.url_entry.get.return_value = "https://unsupported.example/novel/1"
    with patch("gui_components.chapter_editor.messagebox.showwarning") as warn:
        app.start_parse_catalog()
    warn.assert_called_once()



def test_full_gui_window_has_nine_sources_preview_and_queue_site_column():
    root = tk.Tk()
    root.withdraw()
    try:
        # Tk widgets are fully constructed, but no external GitHub account is read.
        app = AudiobookGUIApp(root)
        assert tuple(app.source_combo["values"]) == SOURCE_OPTIONS
        assert app.source_choice_var.get() == AUTO_SOURCE_LABEL
        assert not app.btn_preview_chapter.instate(["disabled"])
        assert not app.btn_sample_text.instate(["disabled"])
        assert not app.btn_start_processing.instate(["disabled"])
        app.source_choice_var.set(SOURCE_NAMES["moon17"])
        app._on_source_selected()
        assert app.url_entry.get() == SOURCE_EXAMPLES["moon17"]

        data = {
            "queue": [{
                "task_id": "book-ui-test", "book_title": "紅樓夢",
                "catalog_url": "https://8book.com/novelbooks/168909/",
                "position": 1, "status": "queued", "run_id": None,
            }],
            "completed": [],
        }
        app._render_queue(data)
        assert app.pending_queue_tree.set("book-ui-test", "source") == "無限小說"
    finally:
        root.destroy()



def test_scrape_workflow_respects_source_specific_parallelism():
    """The GUI's selected site must constrain TXT workers, not just rendering."""
    from pathlib import Path
    workflow = (Path(__file__).resolve().parents[1] /
                ".github" / "workflows" / "audiobook.yml").read_text(encoding="utf-8")
    scrape = workflow.split("\n  scrape_chapters:", 1)[1].split("\n  trigger_scrape_rerun:", 1)[0]
    assert "max-parallel: 17" in scrape
    assert "needs: [setup, resume_planner]" in scrape


def test_source_catalog_example_is_the_live_validated_golden_house_book():
    from tools.validate_sources_live import SAMPLES
    source, gui_url = normalize_source_url(SOURCE_EXAMPLES["hjwzw"])
    other, live_url = normalize_source_url(SAMPLES["hjwzw"])
    assert source.source_id == other.source_id == "hjwzw"
    assert source.book_id(gui_url) == other.book_id(live_url)



def test_catalog_parse_remains_available_when_github_profile_unavailable():
    """A profile API outage must not be mislabeled as a website parser error."""
    from gui_components.source_ui import normalize_source_url
    app = object.__new__(AudiobookGUIApp)
    url = "https://czbooks.net/n/c37492"
    app.url_entry = Mock()
    app.url_entry.get.return_value = url
    app.editing_task_id = None
    app.catalog_load_token = 0
    app.btn_parse = Mock()
    app.lbl_status = Mock()
    app.log = Mock()
    app.root = Mock()
    app.root.after.side_effect = lambda delay, callback: callback()
    app._profile_store = Mock(side_effect=RuntimeError("GitHub 暫時無法連線"))
    app._on_parse_success = Mock()
    app._on_parse_failed = Mock()
    catalog = {
        "success": True, "catalog_url": normalize_source_url(url)[1],
        "book_title": "測試作品", "source_id": "czbooks",
        "total_chapters": 3,
    }

    def immediate_thread(target, daemon=True):
        thread = Mock()
        thread.start.side_effect = target
        return thread

    with patch("gui_components.chapter_editor.parse_catalog", return_value=catalog):
        with patch("gui_components.chapter_editor.threading.Thread", side_effect=immediate_thread):
            app.start_parse_catalog()

    app._on_parse_success.assert_called_once_with(catalog, {})
    app._on_parse_failed.assert_not_called()
    assert "雲端書籍設定" in app.log.call_args.args[0]



def test_tool_panels_are_embedded_without_child_processes():
    from gui_components import tool_launchers
    app = object.__new__(AudiobookGUIApp)
    root = tk.Tk()
    root.withdraw()
    try:
        app.root = root
        app.log = Mock()
        parent = tk.Frame(root)
        app._build_tools_tab_ui(parent)
        assert set(app._tool_panels) == {"merge", "youtube"}
        assert app._tool_instances == {}
        assert app._tool_panels["merge"].winfo_toplevel() == root
        assert app._tool_panels["youtube"].winfo_toplevel() == root
        with patch.object(tool_launchers.importlib, "import_module") as import_module:
            widget = Mock()
            widget.winfo_children.return_value = []
            def make_widget(master):
                return tk.Frame(master)
            import_module.return_value.MergeUploadGUI = make_widget
            assert app._launch_auxiliary_tool("merge") is True
            assert "merge" in app._tool_instances
        assert "subprocess" not in tool_launchers.__dict__
    finally:
        root.destroy()


def test_both_auxiliary_classes_are_tk_frames_and_accept_embedding():
    import importlib
    merge = importlib.import_module("合併上傳.gui")
    youtube = importlib.import_module("tools.youtube_backfill_gui")
    assert issubclass(merge.MergeUploadGUI, ttk.Frame)
    assert issubclass(youtube.App, ttk.Frame)
