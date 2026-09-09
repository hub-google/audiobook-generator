import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "合併上傳"))

from cloud_pipeline import (  # noqa: E402
    YOUTUBE_DESCRIPTION_LIMIT,
    YOUTUBE_TIMELINE_BUDGET,
    output_chapter_timeline,
)
from hf_catalog import HfBook, HfPart  # noqa: E402
from merge_plan import build_plan  # noqa: E402


def output_item(count, title_length=8, duration=60.0):
    return {
        "parts": [{
            "chapter_timeline": [
                {"chap_num": number, "chapter_title": "標題" * title_length, "dur": duration}
                for number in range(1, count + 1)
            ]
        }]
    }


def test_short_timeline_keeps_complete_chapter_titles():
    description = output_chapter_timeline(output_item(5, title_length=3))
    assert "標題標題標題" in description
    assert "第1章" in description
    assert len(description) <= YOUTUBE_TIMELINE_BUDGET


def test_medium_timeline_falls_back_to_chapter_numbers_only():
    description = output_chapter_timeline(output_item(100, title_length=30))
    assert "標題" not in description
    assert "第1章" in description
    assert "第100章" in description
    assert "–" not in description
    assert len(description) <= YOUTUBE_TIMELINE_BUDGET


def test_very_long_timeline_groups_chapters_with_true_first_start_times():
    description = output_chapter_timeline(output_item(1000, title_length=30, duration=61.0))
    lines = description.splitlines()
    assert lines[1] == "00:00:00 第1–5章"
    assert lines[2] == "00:05:05 第6–10章"
    assert lines[-1].endswith("第996–1000章")
    assert len(description) <= YOUTUBE_TIMELINE_BUDGET
    assert len(description) <= YOUTUBE_DESCRIPTION_LIMIT


def test_description_compression_does_not_change_duration_based_merge_plan():
    chapters = output_item(800, title_length=40)["parts"][0]["chapter_timeline"]
    parts = [
        HfPart(1, 1, 400, "book/part-1.mp4", 100, "sha-1", 10 * 3600, chapters[:400]),
        HfPart(2, 401, 800, "book/part-2.mp4", 100, "sha-2", 10 * 3600, chapters[400:]),
        HfPart(3, 801, 1200, "book/part-3.mp4", 100, "sha-3", 10 * 3600, chapters[:400]),
    ]
    book = HfBook("fp", "書", "有聲小說/fp", "revision", parts)

    plan = build_plan(book, 24)
    before = [[part["number"] for part in item["parts"]] for item in plan["outputs"]]
    for item in plan["outputs"]:
        output_chapter_timeline(item)
    after = [[part["number"] for part in item["parts"]] for item in plan["outputs"]]

    assert before == after == [[1, 2], [3]]

