"""YouTube post-publication reconciliation and verification."""

import logging
import os
import sys
import time
from typing import Any

from .errors import UploadPaused, classify_daily_limit
from .media import (
    is_valid_chinese_caption,
    resolve_part_srt,
    set_video_privacy,
    upload_caption_file,
)
from .playlists import add_video_to_playlist, get_playlist_video_index


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def verify_published_part(youtube, video_id, playlist_id, privacy_status, attempts=5,
                          srt_path=None, part_title="", part_num=None,
                          cover_path=None, playlist_position=None):
    """Read YouTube back after writes; API success alone is not final acceptance."""
    time_sleep_fn = _get_symbol("time", time).sleep
    set_privacy_fn = _get_symbol("set_video_privacy", set_video_privacy)
    upload_caption_fn = _get_symbol("upload_caption_file", upload_caption_file)
    get_playlist_index_fn = _get_symbol("get_playlist_video_index", get_playlist_video_index)
    add_playlist_fn = _get_symbol("add_video_to_playlist", add_video_to_playlist)
    resolve_srt_fn = _get_symbol("resolve_part_srt", resolve_part_srt)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            while True:
                try:
                    response = youtube.videos().list(part="status,snippet", id=video_id).execute()
                    break
                except Exception as error:
                    err_str = str(error)
                    if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(error) is True:
                        logging.info("🔄 配額已切換至下一專案，重新讀取影片狀態...")
                        continue
                    paused = classify_daily_limit(error)
                    if paused:
                        raise paused from error
                    raise
            items = response.get("items") or []
            if not items:
                raise RuntimeError(f"uploaded video cannot be read back: {video_id}")
            actual_privacy = (items[0].get("status") or {}).get("privacyStatus")
            if actual_privacy != privacy_status:
                logging.warning("Final reconciliation repairing privacy: Part %s | %s | %s -> %s", part_num, video_id, actual_privacy, privacy_status)
                if not set_privacy_fn(youtube, video_id, privacy_status):
                    raise RuntimeError(f"privacy mismatch: expected {privacy_status}, got {actual_privacy}")
                raise RuntimeError("privacy repaired; waiting for YouTube read-back")
            while True:
                try:
                    captions = youtube.captions().list(part="id,snippet", videoId=video_id).execute().get("items") or []
                    break
                except Exception as error:
                    err_str = str(error)
                    if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(error) is True:
                        logging.info("🔄 配額已切換至下一專案，重新讀取字幕清單...")
                        continue
                    paused = classify_daily_limit(error)
                    if paused:
                        raise paused from error
                    raise
            matching_captions = [
                item.get("snippet") or {}
                for item in captions
                if is_valid_chinese_caption(item.get("snippet") or {})
            ]
            logging.info(
                "Final reconciliation read-back: Part %s | video=%s | captions=%s",
                part_num, video_id,
                [(snippet.get("language"), snippet.get("status"), snippet.get("failureReason"))
                 for snippet in (item.get("snippet") or {} for item in captions)],
            )
            if not matching_captions:
                target_srt = srt_path or resolve_srt_fn(title=part_title, part_num=part_num)
                if target_srt and os.path.exists(target_srt):
                    logging.warning("⚠️ 檢驗發現缺少繁中字幕軌，嘗試自動補上傳：%s", target_srt)
                    if upload_caption_fn(youtube, video_id, os.path.abspath(target_srt)):
                        captions = youtube.captions().list(part="id,snippet", videoId=video_id).execute().get("items") or []
                        matching_captions = [
                            item.get("snippet") or {}
                            for item in captions
                            if is_valid_chinese_caption(item.get("snippet") or {})
                        ]
            if not matching_captions:
                raise RuntimeError("zh-TW caption track cannot be read back")
            failed_captions = [
                caption for caption in matching_captions
                if caption.get("status") == "failed"
            ]
            if failed_captions:
                raise RuntimeError(
                    "zh-TW caption processing failed: "
                    + ", ".join(str(item.get("failureReason") or "unknown") for item in failed_captions)
                )
            if not any(caption.get("status") == "serving" for caption in matching_captions):
                raise RuntimeError("zh-TW caption track is not serving yet")
            playlist_index = get_playlist_index_fn(youtube, playlist_id)
            if video_id not in set(playlist_index.values()):
                logging.warning("Final reconciliation repairing playlist membership: Part %s | %s", part_num, video_id)
                if not add_playlist_fn(youtube, playlist_id, video_id, position=playlist_position):
                    raise RuntimeError("video is missing from the target playlist and repair failed")
                raise RuntimeError("playlist membership repaired; waiting for YouTube read-back")
            return {"youtube_video_id": video_id, "privacy": actual_privacy, "caption_language": "zh-TW"}
        except UploadPaused:
            raise
        except Exception as error:
            last_error = error
            if attempt < attempts:
                time_sleep_fn(min(2 ** attempt, 16))
    raise RuntimeError(f"final YouTube read-back validation failed: {last_error}")
