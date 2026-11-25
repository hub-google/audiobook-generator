"""Pure chapter ordering is independently testable without Tk or GitHub."""
from src.chapter_selection_state import (
    complete_source_order, invert_checked, move_before,
    output_numbers, set_checked, sort_normalized,
)


def test_chapter_order_reconciles_missing_and_duplicate_indexes():
    assert complete_source_order(5, [3, 3, 5, 10, 0]) == [3, 5, 1, 2, 4]
    assert complete_source_order(0, [1]) == []


def test_output_numbers_skip_unchecked_and_keep_original_source_ids():
    order = [5, 1, 3, 2, 4]
    checked = {5: True, 1: False, 3: True, 2: False, 4: True}
    assert output_numbers(order, checked, 2, 5) == {5: 1, 3: 2, 4: 3}
    set_checked(checked, [2, 4], False)
    invert_checked(checked, [2, 4])
    assert checked[2] and checked[4]


def test_multi_selection_drag_and_numeric_order_stay_stable():
    assert move_before([1, 2, 3, 4, 5], [2, 4], 5) == [1, 3, 2, 4, 5]
    assert move_before([1, 2, 3], [1], 1) == [1, 2, 3]
    parts = [
        {"normalized_number":"2"},
        {"normalized_number":"1.5"},
        {"normalized_number":""},
        {"normalized_number":"abc"},
        {"normalized_number":"1.5"},
    ]
    assert sort_normalized([5, 2, 1, 4, 3],parts) == [5, 2, 1, 4, 3]
