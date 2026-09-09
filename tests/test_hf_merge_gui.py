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
