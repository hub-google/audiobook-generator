"""Chapter table rows reflect filtered selection and production numbers."""
from unittest.mock import Mock

from gui_components.chapter_table_ui import render_chapter_rows


def test_chapter_table_preserves_source_uuids_and_filtered_output_numbers():
    tree = Mock()
    tree.get_children.return_value = ()
    parts = [
        {"normalized_number": "3", "display_number": "第3章", "chapter_name": "結尾"},
        {"normalized_number": "1", "display_number": "第1章", "chapter_name": "開始"},
        {"normalized_number": "2", "display_number": "第2章", "chapter_name": "中段"},
    ]
    visible = render_chapter_rows(
        tree, [2, 3, 1], parts, {1: True, 2: True, 3: False},
        1, 3, 3, False, False, set(), "開始", {2:1, 1:2},
    )
    assert visible == [2]
    assert tree.insert.call_count == 1
    assert tree.insert.call_args.kwargs["iid"] == "2"
    assert tree.insert.call_args.kwargs["values"][1] == "☑ 1"


def test_duplicate_only_view_keeps_duplicates_even_when_unselected():
    tree = Mock()
    tree.get_children.return_value = ()
    parts = [
        {"normalized_number": "1", "display_number": "第1章", "chapter_name": "重複"},
        {"normalized_number": "1", "display_number": "第1章", "chapter_name": "重複"},
    ]
    visible = render_chapter_rows(
        tree, [1, 2], parts, {1: True, 2: False},
        1, 2, 2, True, True, {1, 2}, "", {1:1},
    )
    assert visible == [1, 2]
    assert tree.insert.call_args.kwargs["values"][1] == "☐"
    assert tree.insert.call_args.kwargs["values"][-1] == "重複"
