import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "合併上傳"))

from hf_catalog import HfBook, HfPart, duration_seconds
from merge_plan import build_plan, group_parts


def part(number, hours, start=1, end=10):
    return HfPart(number, start, end, f"book/part-{number}.mp4", 100 * number, f"sha-{number}", hours * 3600)


def test_duration_comes_from_ffprobe_format():
    assert duration_seconds({"format": {"duration": "3600.25"}}) == 3600.25


def test_groups_are_contiguous_and_never_exceed_limit():
    parts = [part(1, 10), part(2, 10), part(3, 7.8), part(4, 8.5)]
    groups = group_parts(parts, 24)
    assert [[p.number for p in group] for group in groups] == [[1, 2], [3, 4]]
    assert all(sum(p.duration for p in group) <= 24 * 3600 for group in groups)


def test_example_stops_before_part_that_crosses_48_hours():
    parts = [part(number, 47.833 / 123) for number in range(1, 124)]
    parts.append(part(124, 0.667))
    groups = group_parts(parts, 48)
    assert groups[0][-1].number == 123
    assert groups[1][0].number == 124


def test_rejects_source_part_longer_than_cap():
    try:
        group_parts([part(1, 10.2)], 8)
    except ValueError as error:
        assert "不會被拆開" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_all_mode_builds_one_stable_output():
    book = HfBook("fp", "書", "有聲小說/fp", "revision", [part(1, 10), part(2, 9)])
    first, second = build_plan(book, None), build_plan(book, None)
    assert len(first["outputs"]) == 1
    assert first["plan_id"] == second["plan_id"]
    assert first["outputs"][0]["youtube_title"] == "[已完結]《書》第 1~10 章"


def test_split_outputs_show_the_same_numbered_titles_youtube_will_receive():
    book = HfBook("fp", "書", "有聲小說/fp", "revision", [
        part(1, 10, 1, 115), part(2, 10, 116, 229), part(3, 10, 230, 344),
    ])
    outputs = build_plan(book, 15)["outputs"]
    assert [item["youtube_title"] for item in outputs] == [
        "[已完結]《書》第 1~115 章【第 1 部】",
        "[已完結]《書》第 116~229 章【第 2 部】",
        "[已完結]《書》第 230~344 章【第 3 部】",
    ]
