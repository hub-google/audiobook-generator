"""Pure YouTube title, chapter-timeline, and description builders."""

try:
    from ..catalog_parser import format_output_chapter_title
except ImportError:  # Direct execution through src/youtube_api_uploader.py.
    from catalog_parser import format_output_chapter_title


def _chapter_title(item):
    number = int(item["chap_num"])
    return format_output_chapter_title(number, item.get("chapter_title") or "")


def build_chapter_timeline(chapter_items):
    """Build YouTube chapters without accumulating per-chapter rounding error."""
    items = list(chapter_items or [])
    if len(items) < 3:
        raise ValueError("YouTube chapter timeline requires at least three chapters")
    lines = []
    exact_start = 0.0
    previous_second = -1
    for position, item in enumerate(items):
        duration = float(item.get("dur") or 0.0)
        if duration < 10.0:
            raise ValueError(f"第 {item.get('chap_num')} 章長度少於 YouTube 規定的 10 秒")
        display_second = 0 if position == 0 else int(exact_start + 0.5)
        if display_second <= previous_second:
            raise ValueError("chapter timestamps are not strictly increasing")
        if abs(display_second - exact_start) >= 1.0:
            raise ValueError("chapter timestamp differs from its media boundary by one second or more")
        hours, remainder = divmod(display_second, 3600)
        minutes, seconds = divmod(remainder, 60)
        lines.append(f"{hours:02d}:{minutes:02d}:{seconds:02d} {_chapter_title(item)}")
        previous_second = display_second
        exact_start += duration
    return "⏳ 影片章節時間軸：\n" + "\n".join(lines)


def build_video_description(book_title, description, playlist_id, chapter_items=None):
    """Build a publishable description and an optional valid chapter timeline."""
    title = str(book_title or "").strip()
    playlist = str(playlist_id or "").strip()
    if not title:
        raise ValueError("book title is required for the YouTube description")
    if not playlist:
        raise ValueError("playlist id is required for the YouTube description")
    sections = [
        f"▶️《{title}》播放清單全集\n"
        f"https://www.youtube.com/playlist?list={playlist}"
    ]
    if chapter_items is not None and len(chapter_items) >= 3:
        sections.append(build_chapter_timeline(chapter_items))
    return "\n\n".join(sections)


def part_number_for_title(part_plan, title):
    planned = next((part for part in part_plan if str(part.get("title") or "") == str(title)), None)
    return int(planned["part_num"]) if planned else None
