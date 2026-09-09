"""Deterministic duration-capped grouping for archived audiobook Parts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from hf_catalog import HfBook, HfPart


def youtube_video_title(book_title: str, start_chapter: int, end_chapter: int,
                        output_number: int | None = None, output_count: int = 1) -> str:
    """Return the exact title shared by the GUI preview and YouTube upload."""
    part = f"【第 {int(output_number)} 部】" if output_count > 1 and output_number else ""
    return f"[已完結]《{book_title}》第 {int(start_chapter)}~{int(end_chapter)} 章{part}"


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_size(size: int) -> str:
    return f"{size / (1024 ** 3):.2f} GB"


def group_parts(parts: list[HfPart], max_hours: float | None) -> list[list[HfPart]]:
    if not parts:
        raise ValueError("沒有可合併的 Part")
    if max_hours is None:
        return [list(parts)]
    limit = float(max_hours) * 3600
    if limit <= 0:
        raise ValueError("每支影片時數必須大於 0")
    too_long = next((part for part in parts if part.duration > limit + 0.001), None)
    if too_long:
        raise ValueError(
            f"Part {too_long.number:02d} 時長 {format_duration(too_long.duration)} 已超過設定上限 "
            f"{format_duration(limit)}；請提高時數，因為原始 Part 不會被拆開。"
        )
    groups, current, elapsed = [], [], 0.0
    for part in parts:
        if current and elapsed + part.duration > limit + 0.001:
            groups.append(current)
            current, elapsed = [], 0.0
        current.append(part)
        elapsed += part.duration
    if current:
        groups.append(current)
    return groups


def build_plan(book: HfBook, max_hours: float | None) -> dict:
    groups = group_parts(book.parts, max_hours)
    outputs = []
    for number, group in enumerate(groups, 1):
        outputs.append({
            "output_number": number,
            "part_start": group[0].number, "part_end": group[-1].number,
            "start_chapter": group[0].start_chapter, "end_chapter": group[-1].end_chapter,
            "duration_seconds": sum(item.duration for item in group),
            "bytes": sum(item.video_bytes for item in group),
            "parts": [asdict(item) for item in group],
        })
    for item in outputs:
        item["youtube_title"] = youtube_video_title(
            book.title, item["start_chapter"], item["end_chapter"],
            item["output_number"], len(outputs),
        )
    plan = {"schema_version": 1, "repo_revision": book.revision, "book_key": book.key,
            "book_title": book.title, "book_root": book.root,
            "mode": "all" if max_hours is None else "max_hours", "max_hours": max_hours,
            "outputs": outputs}
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    plan["plan_id"] = hashlib.sha256(encoded).hexdigest()[:20]
    return plan
