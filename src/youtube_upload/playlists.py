"""YouTube playlist operations, ordering, metadata and verification."""

import glob
import json
import logging
import os
import re
import sys
import time
from typing import Any

from googleapiclient.http import HttpError

from .errors import (
    UploadPaused,
    VideoNotFoundError,
    classify_daily_limit,
    is_transient_youtube_api_error,
)

try:
    from ..part_builder import get_media_duration
except ImportError:
    from part_builder import get_media_duration


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def parse_chapter_info(filename):
    """從檔名解析起始與結束章節號碼，供升序排序"""
    m_range = re.search(r'(?:chapter_|ch)(\d+)_to_(?:chapter_|ch)?(\d+)', filename, re.IGNORECASE)
    if m_range:
        return int(m_range.group(1)), int(m_range.group(2))

    m_single = re.search(r'(?:chapter_|ch)(\d+)', filename, re.IGNORECASE)
    if m_single:
        chap = int(m_single.group(1))
        return chap, chap

    m_worker = re.search(r'worker-(\d+)', filename, re.IGNORECASE)
    if m_worker:
        w_id = int(m_worker.group(1))
        return w_id * 120 + 1, (w_id + 1) * 120

    return 999999, 999999


def get_or_create_playlist(youtube, playlist_title, playlist_desc="", alternate_titles=None, source_fingerprint=""):
    """Return ``(playlist_id, created)`` for the requested playlist."""
    accepted_titles = {str(playlist_title).strip()}
    accepted_titles.update(
        str(title).strip() for title in (alternate_titles or []) if str(title).strip()
    )
    while True:
        logging.info(f"🔍 檢查 YouTube 頻道是否存在播放清單:【{playlist_title}】...")
        try:
            page_token = None
            while True:
                response = youtube.playlists().list(
                    part="snippet,status", mine=True, maxResults=50,
                    pageToken=page_token,
                ).execute()
                for item in response.get("items", []):
                    if source_fingerprint and f"來源識別：{source_fingerprint}" not in item["snippet"].get("description", ""):
                        continue
                    if item["snippet"]["title"].strip() in accepted_titles:
                        playlist_id = item["id"]
                        logging.info(f"✅ 找到已有播放清單 (ID: {playlist_id}):【{playlist_title}】")
                        return playlist_id, False
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

            logging.info(f"➕ 正在建立全新播放清單:【{playlist_title}】...")
            body = {
                "snippet": {
                    "title": playlist_title,
                    "description": playlist_desc,
                    "defaultLanguage": "zh-TW"
                },
                "status": {
                    "privacyStatus": "public"
                }
            }
            create_res = youtube.playlists().insert(
                part="snippet,status",
                body=body
            ).execute()
            playlist_id = create_res["id"]
            logging.info(f"🎉 成功建立播放清單 (ID: {playlist_id}):【{playlist_title}】")
            return playlist_id, True
        except Exception as e:
            if ("quotaExceeded" in str(e) or "dailyLimitExceeded" in str(e)) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新查詢/建立播放清單...")
                continue
            logging.error(f"❌ 查詢/建立播放清單失敗: {e}")
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            return None, False


def completed_playlist_title(book_title, total_duration_seconds):
    """Build the final title from measured video duration; never estimate it."""
    total_seconds = float(total_duration_seconds)
    if total_seconds < 0:
        raise ValueError("total playlist duration cannot be negative")
    whole_hours = int(total_seconds // 3600)
    return f"[已完結]《{book_title}》{whole_hours}小時 全集"


def load_measured_prepared_part_plan(input_dir, book_title):
    """Load locked prepared Parts and replace planned durations with MP4 measurements."""
    try:
        from ..metadata_gen import save_book_metadata as build_part_metadata
    except ImportError:
        from metadata_gen import save_book_metadata as build_part_metadata

    media_duration_fn = _get_symbol("get_media_duration", get_media_duration)

    if not input_dir:
        return []
    plan_path = os.path.join(input_dir, "parts-plan.json")
    if not os.path.isfile(plan_path):
        return []
    with open(plan_path, "r", encoding="utf-8") as handle:
        locked_plan = json.load(handle)
    locked_parts = {int(part["part_num"]): part for part in (locked_plan.get("parts") or [])}
    measured = []
    for video_path in glob.glob(os.path.join(input_dir, "**", "*.mp4"), recursive=True):
        filename = os.path.basename(video_path)
        number_match = re.search(r"_Part_(\d+)_", filename)
        if not number_match:
            continue
        part_num = int(number_match.group(1))
        locked = locked_parts.get(part_num)
        if not locked:
            raise RuntimeError(f"prepared Part {part_num} is absent from the locked plan")
        start_chap, end_chap = parse_chapter_info(filename)
        if (int(locked["start_chap"]), int(locked["end_chap"])) != (start_chap, end_chap):
            raise RuntimeError(f"prepared Part {part_num} filename disagrees with locked plan")
        duration = float(media_duration_fn(video_path) or 0)
        if duration <= 0:
            raise RuntimeError(f"prepared Part {part_num} has no measured MP4 duration")
        metadata = build_part_metadata(
            book_title=book_title, start_chap=start_chap, end_chap=end_chap,
            is_completed=True, part_num=part_num,
        )
        measured.append({
            "part_num": part_num, "start_chap": start_chap, "end_chap": end_chap,
            "chapters": [int(value) for value in locked.get("chapters") or range(start_chap, end_chap + 1)],
            "duration": duration, "title": metadata["title"],
        })
    measured.sort(key=lambda part: int(part["part_num"]))
    if set(locked_parts) != {int(part["part_num"]) for part in measured}:
        raise RuntimeError("HF prepared Parts do not exactly cover the locked Part plan")
    return measured


def update_playlist_metadata(youtube, playlist_id, title, description,
                             network_attempts=5, initial_retry_delay=2):
    """Update an existing playlist after every video has passed final validation."""
    transient_failures = 0
    sleep_fn = _get_symbol("time", time).sleep
    while True:
        try:
            youtube.playlists().update(
                part="snippet",
                body={
                    "id": playlist_id,
                    "snippet": {
                        "title": title,
                        "description": description,
                        "defaultLanguage": "zh-TW",
                    },
                },
            ).execute()
            logging.info("✅ 播放清單正式標題已更新：【%s】", title)
            return True
        except Exception as e:
            if ("quotaExceeded" in str(e) or "dailyLimitExceeded" in str(e)) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新更新播放清單標題...")
                continue
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            if is_transient_youtube_api_error(e) and transient_failures < network_attempts - 1:
                delay = min(initial_retry_delay * (2 ** transient_failures), 60)
                transient_failures += 1
                logging.warning(
                    "YouTube 暫時無法更新播放清單（第 %s/%s 次）；%s 秒後重試：%s",
                    transient_failures, network_attempts, delay, e,
                )
                sleep_fn(delay)
                continue
            raise RuntimeError(f"更新播放清單正式標題失敗：{e}") from e


def add_video_to_playlist(youtube, playlist_id, video_id, position=None):
    """將影片加到指定的播放清單中 (依呼叫順序追加)"""
    visibility_failures = 0
    visibility_attempts = 5
    initial_visibility_delay = 2
    sleep_fn = _get_symbol("time", time).sleep
    while True:
        try:
            body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
            if position is not None:
                body["snippet"]["position"] = int(position)
            response = youtube.playlistItems().insert(
                part="snippet",
                body=body
            ).execute()
            logging.info(f"📋 成功將影片 [Video ID: {video_id}] 加入播放清單！")
            return response.get("id") or True
        except Exception as e:
            err_content = getattr(e, "content", b"")
            if isinstance(err_content, bytes):
                err_content = err_content.decode("utf-8", errors="replace")
            err_str = f"{e} {err_content}"
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新加入播放清單...")
                continue
            if "videoNotFound" in err_str:
                visibility_failures += 1
                if visibility_failures < visibility_attempts:
                    delay = min(initial_visibility_delay * (2 ** (visibility_failures - 1)), 16)
                    logging.warning(
                        "影片尚未對 API 專案可見；%s 秒後重試加入播放清單 (%s/%s)。",
                        delay, visibility_failures, visibility_attempts,
                    )
                    sleep_fn(delay)
                    continue
                logging.error(f"❌ 影片 [Video ID: {video_id}] 在 YouTube 上不存在 (videoNotFound)！")
                raise VideoNotFoundError(f"Video {video_id} not found on YouTube", video_id=video_id, original_error=e) from e
            logging.warning(f"⚠️ 將影片 [Video ID: {video_id}] 加入播放清單失敗: {e}")
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            return False


def get_existing_playlist_video_titles(youtube, playlist_id):
    """獲取播放清單中已存在的影片標題集合，實現斷點續傳與自動跳過"""
    if not playlist_id:
        return set()
    titles = set()
    next_page_token = None
    while True:
        try:
            res = youtube.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=next_page_token
            ).execute()
            for item in res.get("items", []):
                t = item["snippet"]["title"].strip()
                titles.add(t)
            next_page_token = res.get("nextPageToken")
            if not next_page_token:
                break
        except Exception as e:
            err_str = str(e)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新獲取播放清單影片標題...")
                continue
            if "playlistNotFound" in err_str:
                logging.info("📋 播放清單全新建立完成，目前為空狀態。")
                break
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            logging.warning(f"無法獲取播放清單既有影片清單: {e}")
            break
    logging.info(f"📋 成功獲取播放清單已有 {len(titles)} 部影片，開啟【智能斷點續傳】跳過機制！")
    return titles


def get_playlist_video_index(youtube, playlist_id, attempts=5, initial_delay=2):
    """Return playlist title -> video id for checkpoint migration/repair."""
    index = {}
    if not playlist_id:
        return index
    next_page_token = None
    sleep_fn = _get_symbol("time", time).sleep
    while True:
        res = None
        for attempt in range(attempts):
            try:
                res = youtube.playlistItems().list(
                    part="snippet", playlistId=playlist_id, maxResults=50,
                    pageToken=next_page_token,
                ).execute()
                break
            except Exception as error:
                err_str = str(error)
                if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(error) is True:
                    logging.info("🔄 配額已切換至下一專案，重新獲取播放清單索引...")
                    continue
                paused = classify_daily_limit(error)
                if paused:
                    raise paused from error
                is_not_found = (
                    isinstance(error, HttpError)
                    and getattr(getattr(error, "resp", None), "status", None) == 404
                    or "playlistNotFound" in err_str
                )
                if not is_not_found or attempt == attempts - 1:
                    raise
                delay = initial_delay * (2 ** attempt)
                logging.warning(
                    "播放清單尚未同步完成；%s 秒後重試 (%s/%s)。",
                    delay, attempt + 1, attempts,
                )
                sleep_fn(delay)
        if res is None:
            raise RuntimeError("無法獲取播放清單索引")
        for item in res.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            title = str(snippet.get("title") or "").strip()
            if title and video_id:
                index[title] = video_id
        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            return index


def get_ordered_playlist_items(youtube, playlist_id):
    """依照實際位置讀取使用者看到的完整清單，不可吞掉重複或失效項目。"""
    items = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50,
            pageToken=page_token,
        ).execute()
        for raw in response.get("items", []):
            snippet = raw.get("snippet") or {}
            items.append({
                "playlist_item_id": str(raw.get("id") or ""),
                "position": int(snippet.get("position", len(items))),
                "title": str(snippet.get("title") or "").strip(),
                "video_id": str((snippet.get("resourceId") or {}).get("videoId") or "").strip(),
            })
        page_token = response.get("nextPageToken")
        if not page_token:
            return sorted(items, key=lambda item: item["position"])


def validate_user_facing_playlist(playlist_items, part_plan):
    """驗證使用者看到的 Part 數量、排序與影片唯一性。"""
    ordered_plan = sorted(part_plan, key=lambda part: int(part["part_num"]))
    expected_numbers = list(range(1, len(ordered_plan) + 1))
    actual_numbers = [int(part["part_num"]) for part in ordered_plan]
    if actual_numbers != expected_numbers:
        raise RuntimeError(
            f"user-facing playlist validation failed: Part plan must be contiguous 1..{len(ordered_plan)}; "
            f"got {actual_numbers}"
        )

    expected_titles = [str(part.get("title") or "").strip() for part in ordered_plan]
    actual_titles = [str(item.get("title") or "").strip() for item in playlist_items]
    if len(actual_titles) != len(expected_titles):
        raise RuntimeError(
            "user-facing playlist validation failed: playlist must contain exactly "
            f"{len(expected_titles)} Parts, but viewers see {len(actual_titles)} items"
        )
    if actual_titles != expected_titles:
        mismatch = next(
            (index for index, pair in enumerate(zip(expected_titles, actual_titles), 1)
             if pair[0] != pair[1]), None,
        )
        raise RuntimeError(
            "user-facing playlist validation failed: Parts are missing, duplicated, or out of order"
            + (f"; first mismatch is position {mismatch}" if mismatch else "")
        )

    positions = [int(item.get("position", -1)) for item in playlist_items]
    if positions != list(range(len(expected_titles))):
        raise RuntimeError(f"user-facing playlist validation failed: invalid positions {positions}")
    video_ids = [str(item.get("video_id") or "").strip() for item in playlist_items]
    if any(not video_id for video_id in video_ids) or len(set(video_ids)) != len(video_ids):
        raise RuntimeError(
            "user-facing playlist validation failed: deleted/unavailable or duplicate videos are present"
        )

    return {
        "status": "passed",
        "item_count": len(playlist_items),
        "ordered_parts": expected_numbers,
        "unique_video_ids": len(set(video_ids)),
    }


def get_channel_upload_video_index(youtube):
    """Return title -> video id for the authenticated channel's uploads."""
    while True:
        try:
            channels = youtube.channels().list(part="contentDetails", mine=True).execute()
            break
        except Exception as e:
            err_str = str(e)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新查詢頻道上傳清單...")
                continue
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            raise
    items = channels.get("items", [])
    if not items:
        return {}
    uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    get_playlist_index_fn = _get_symbol("get_playlist_video_index", get_playlist_video_index)
    return get_playlist_index_fn(youtube, uploads_id)
