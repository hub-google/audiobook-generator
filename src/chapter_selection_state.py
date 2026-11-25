"""Pure chapter selection and ordering functions for the Tk editor.

Source chapter IDs remain distinct from the contiguous production order.
No Tk widgets, networking or persistence in these functions.
"""
from decimal import Decimal, InvalidOperation


def complete_source_order(chapter_count, saved_order=()):
    count = int(chapter_count)
    if count < 0:
        raise ValueError("negative chapter count")
    order = []
    seen = set()
    for raw in (*saved_order, *range(1, count + 1)):
        index = int(raw)
        if 1 <= index <= count and index not in seen:
            order.append(index)
            seen.add(index)
    return order


def output_numbers(order, checked, first, last):
    return {
        index: serial
        for serial, index in enumerate(
            (index for index in order if first <= index <= last and checked.get(index, True)), 1,
        )
    }


def move_before(order, moving, target):
    selected = set(moving)
    if not selected or target in selected or target not in order:
        return list(order)
    original = [index for index in order if index in selected]
    remaining = [index for index in order if index not in selected]
    insertion = remaining.index(target)
    return remaining[:insertion] + original + remaining[insertion:]


def sort_normalized(order, chapter_parts):
    """Stable numeric sort, leaving missing and invalid source numbers last."""
    positions = {value: position for position, value in enumerate(order)}

    def key(source_index):
        value = chapter_parts[source_index - 1]["normalized_number"]
        try:
            return 0, Decimal(str(value)), positions[source_index]
        except (InvalidOperation, ValueError):
            return 1, Decimal(0), positions[source_index]

    return sorted(order, key=key)


def set_checked(checked, indexes, value):
    for index in indexes:
        checked[index] = bool(value)


def invert_checked(checked, indexes):
    for index in indexes:
        checked[index] = not checked[index]
