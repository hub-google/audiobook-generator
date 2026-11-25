"""Duplicate review windows have explicit context instead of hidden closure state."""
import inspect
from gui_components import duplicate_review_dialogs


def test_duplicate_windows_are_separate_from_chapter_editor_and_explicit():
    details = inspect.signature(duplicate_review_dialogs.show_duplicate_details)
    options = inspect.signature(duplicate_review_dialogs.choose_duplicate_conditions)
    assert "overrides" in details.parameters
    assert "duplicate_count" in options.parameters
    assert "refresh" in options.parameters
    assert "render" in options.parameters
