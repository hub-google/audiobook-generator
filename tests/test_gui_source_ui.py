"""No network: nine-site GUI catalog routing, safety and batch handling."""
import tkinter as tk
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
    assert "同站最多" in source_hint(adapter)
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
