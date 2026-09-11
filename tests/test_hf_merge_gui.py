import importlib.util
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
MERGE_DIR = ROOT / "合併上傳"


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, MERGE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_book_sort_values_use_raw_numeric_metadata(monkeypatch):
    monkeypatch.syspath_prepend(str(MERGE_DIR))
    gui = load_module("gui")
    catalog = load_module("hf_catalog")
    part = catalog.HfPart(1, 9, 12, "x.mp4", 2_000, "sha", 80.0)
    book = catalog.HfBook("book", "Book 10", "有聲小說/book", "rev", [part])
    assert gui.book_sort_value(book, "parts") == 1
    assert gui.book_sort_value(book, "chapters") == (9, 12)
    assert gui.book_sort_value(book, "duration") == 80.0
    assert gui.book_sort_value(book, "size") == 2_000


def test_delete_token_is_separate_from_regular_hf_token(monkeypatch):
    monkeypatch.syspath_prepend(str(MERGE_DIR))
    gui = load_module("gui")
    monkeypatch.setenv("HF_TOKEN", "regular-token")
    monkeypatch.setenv("HF_DELETE_TOKEN", "delete-token")
    assert gui.resolve_hf_delete_token() == "delete-token"


def test_delete_token_does_not_fall_back_to_regular_token(monkeypatch):
    monkeypatch.syspath_prepend(str(MERGE_DIR))
    gui = load_module("gui")
    monkeypatch.setenv("HF_TOKEN", "regular-token")
    monkeypatch.delenv("HF_DELETE_TOKEN", raising=False)
    try:
        gui.resolve_hf_delete_token()
    except ValueError as exc:
        assert "HF_DELETE_TOKEN" in str(exc)
    else:
        raise AssertionError("regular HF_TOKEN was incorrectly accepted for deletion")


def test_delete_book_removes_only_exact_book_folder_and_invalidates_cache(tmp_path):
    catalog = load_module("hf_catalog")
    instance = catalog.HfCatalog.__new__(catalog.HfCatalog)
    instance.api = Mock(); instance.repo_id = "owner/archive"; instance.cache_path = tmp_path / "cache.json"
    instance.cache_path.write_text("cached", encoding="utf-8")
    book = catalog.HfBook("safe-key", "測試書", "有聲小說/safe-key", "rev")
    instance.delete_book(book)
    instance.api.delete_folder.assert_called_once_with(
        path_in_repo="有聲小說/safe-key", repo_id="owner/archive", repo_type="dataset",
        commit_message="Remove completed audiobook archive: 測試書",
    )
    assert not instance.cache_path.exists()


def test_delete_book_rejects_broad_or_mismatched_paths(tmp_path):
    catalog = load_module("hf_catalog")
    instance = catalog.HfCatalog.__new__(catalog.HfCatalog)
    instance.api = Mock(); instance.repo_id = "owner/archive"; instance.cache_path = tmp_path / "cache.json"
    for key, root in (("safe-key", "有聲小說"), ("../escape", "有聲小說/../escape"), ("safe-key", "other/safe-key")):
        try:
            instance.delete_book(catalog.HfBook(key, "測試書", root, "rev"))
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {root}")
    instance.api.delete_folder.assert_not_called()


def test_gui_provides_immediate_resume_scan_dispatch():
    text = (ROOT / "合併上傳" / "gui.py").read_text(encoding="utf-8")
    assert '立即掃描續傳排程' in text
    assert 'dispatch_resume_scheduler' in text
    assert 'hf-upload-resume-scheduler.yml' in text


def test_schedule_preset_computation(monkeypatch):
    monkeypatch.syspath_prepend(str(MERGE_DIR))
    gui = load_module("gui")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    base = datetime(2026, 9, 11, 10, 0, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    preset = gui.compute_schedule_preset("3 天後 18:00", base_dt=base)
    assert preset == "2026-09-14 18:00"
    preset_7 = gui.compute_schedule_preset("7 天後 18:00", base_dt=base)
    assert preset_7 == "2026-09-18 18:00"


def test_validate_and_convert_schedule(monkeypatch):
    monkeypatch.syspath_prepend(str(MERGE_DIR))
    gui = load_module("gui")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 11, 8, 0, 0, tzinfo=ZoneInfo("Asia/Taipei"))

    # Valid: 3 days later 18:00 (72 + 10 = 82 hours in future > 25 hours)
    utc_str = gui.validate_and_convert_schedule("2026-09-14 18:00", now_dt=now, min_hours=25.0)
    assert utc_str == "2026-09-14T10:00:00Z"

    # Too soon: only 10 hours later
    try:
        gui.validate_and_convert_schedule("2026-09-11 18:00", now_dt=now, min_hours=25.0)
    except ValueError as exc:
        assert "預約發布時間過近" in str(exc)
    else:
        raise AssertionError("should have rejected schedule time that is too soon")

    # Past time
    try:
        gui.validate_and_convert_schedule("2026-09-10 18:00", now_dt=now, min_hours=25.0)
    except ValueError as exc:
        assert "必須是未來的時間" in str(exc)
    else:
        raise AssertionError("should have rejected past schedule time")

    # Invalid format
    try:
        gui.validate_and_convert_schedule("invalid-datetime", now_dt=now)
    except ValueError as exc:
        assert "格式錯誤" in str(exc)
    else:
        raise AssertionError("should have rejected malformed schedule time")


def test_gui_contains_schedule_controls_and_dispatch():
    text = (ROOT / "合併上傳" / "gui.py").read_text(encoding="utf-8")
    assert "預約公開（排程發布）" in text
    assert "SCHEDULE_PRESETS" in text
    assert "publish_at" in text


def test_gui_has_vertical_scrollbar_and_scrollable_canvas():
    text = (ROOT / "合併上傳" / "gui.py").read_text(encoding="utf-8")
    assert "self.canvas = tk.Canvas" in text
    assert "self.v_scrollbar = ttk.Scrollbar" in text
    assert "yscrollcommand=self.v_scrollbar.set" in text
    assert "self.scroll_content" in text
    assert "<MouseWheel>" in text


def test_gui_initializes_canvas_and_widgets(monkeypatch):
    monkeypatch.syspath_prepend(str(MERGE_DIR))
    gui = load_module("gui")
    monkeypatch.setattr(gui.MergeUploadGUI, "refresh_books", lambda self, force=False: None)
    app = gui.MergeUploadGUI()
    try:
        app.update_idletasks()
        assert app.canvas is not None
        assert app.v_scrollbar is not None
        assert app.scroll_content is not None
        assert app.start_btn is not None
        assert app.open_btn is not None
        bbox = app.canvas.bbox("all")
        assert bbox is not None
        content_height = bbox[3] - bbox[1]
        assert 500 < content_height < 1500
    finally:
        app.destroy()



