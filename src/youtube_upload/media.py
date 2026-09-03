"""YouTube media uploading (videos, captions, thumbnails), privacy and subtitle processing."""

import hashlib
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.http import MediaFileUpload

from .errors import (
    UploadPaused,
    VideoNotFoundError,
    classify_daily_limit,
    is_transient_upload_error,
    is_transient_youtube_api_error,
)

_last_thumbnail_request_at = None
THUMBNAIL_MIN_INTERVAL_SECONDS = 10.0


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def _file_sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def set_video_thumbnail(youtube, video_id, cover_path, attempts=None):
    """Apply a custom thumbnail or raise a resumable pause if quota/rate-limited."""
    global _last_thumbnail_request_at
    if not cover_path or not os.path.exists(cover_path):
        logging.warning("⚠️ 封面檔不存在：%s，略過封面設定", cover_path)
        return False

    slot_rounds = _get_symbol("YOUTUBE_SLOT_ROTATION_ROUNDS", 3)
    account_count = max(1, len(getattr(youtube, "accounts", []) or []))
    max_attempts = attempts if attempts is not None else account_count * slot_rounds
    last_error = None

    time_mod = _get_symbol("time", time)
    media_file_cls = _get_symbol("MediaFileUpload", MediaFileUpload)
    min_interval = _get_symbol("THUMBNAIL_MIN_INTERVAL_SECONDS", THUMBNAIL_MIN_INTERVAL_SECONDS)

    for attempt in range(max_attempts):
        try:
            cur_last_thumb = _get_symbol("_last_thumbnail_request_at", _last_thumbnail_request_at)
            if cur_last_thumb is not None:
                elapsed = time_mod.monotonic() - cur_last_thumb
                remaining = min_interval - elapsed
                if remaining > 0:
                    logging.info(
                        "🕒 縮圖頻道頻率保護：等待 %.1f 秒後再呼叫 thumbnails.set…",
                        remaining,
                    )
                    time_mod.sleep(remaining)
            now_mono = time_mod.monotonic()
            _last_thumbnail_request_at = now_mono
            uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
            if uploader is not None:
                setattr(uploader, "_last_thumbnail_request_at", now_mono)

            youtube.thumbnails().set(
                videoId=video_id,
                media_body=media_file_cls(cover_path),
            ).execute()
            logging.info(f"🖼️ 成功為影片 [Video ID: {video_id}] 更新高畫質封面縮圖！")
            return True
        except Exception as e:
            last_error = e
            err_str = str(e)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新嘗試設定封面縮圖...")
                continue
            if "uploadRateLimitExceeded" in err_str or "429" in err_str:
                retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                logging.warning(
                    "⚠️ 頻道縮圖速率限制（429 uploadRateLimitExceeded）；冷卻至 %s。",
                    retry_at.isoformat(),
                )
                raise UploadPaused("thumbnailRateLimit", retry_at, e) from e
            if "videoNotFound" in err_str:
                logging.error(f"❌ 影片 [Video ID: {video_id}] 在 YouTube 上不存在 (videoNotFound)！")
                raise VideoNotFoundError(f"Video {video_id} not found on YouTube", video_id=video_id, original_error=e) from e
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            if is_transient_youtube_api_error(e):
                logging.warning("⚠️ 設定封面縮圖暫時性網路錯誤: %s；重試中...", e)
                time_mod.sleep(2)
                continue
            logging.error(f"❌ 設定封面縮圖失敗 [Video ID: {video_id}]: {e}")
            raise

    if last_error:
        paused = classify_daily_limit(last_error)
        if paused:
            raise paused from last_error
        raise last_error
    return False


def upload_video_file(youtube, video_path, title, description, category_id="22",
                      privacy_status="public", cover_path=None,
                      network_attempts=8, initial_retry_delay=2):
    """使用 Resumable 上傳 MP4 到 YouTube"""
    media_file_cls = _get_symbol("MediaFileUpload", MediaFileUpload)
    time_mod = _get_symbol("time", time)
    os_mod = _get_symbol("os", os)

    while True:
        file_size_mb = os_mod.path.getsize(video_path) / (1024 * 1024)
        logging.info(f"📤 開始 API 極速上傳影片: {title} (檔案大小: {file_size_mb:.1f} MB)...")
        sys.stdout.flush()

        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            }
        }

        media = media_file_cls(video_path, chunksize=10*1024*1024, resumable=True)
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        last_logged_pct = -10
        start_time = time_mod.time()
        consecutive_network_failures = 0

        try:
            while response is None:
                try:
                    status, response = request.next_chunk(num_retries=3)
                    consecutive_network_failures = 0
                except Exception as error:
                    if not is_transient_upload_error(error):
                        raise
                    consecutive_network_failures += 1
                    if consecutive_network_failures >= network_attempts:
                        raise
                    delay = min(
                        initial_retry_delay * (2 ** (consecutive_network_failures - 1)),
                        60,
                    )
                    logging.warning(
                        "上傳連線暫時中斷：%s；%s 秒後從斷點重試 (%s/%s)。",
                        error, delay, consecutive_network_failures, network_attempts,
                    )
                    time_mod.sleep(delay)
                    continue
                if status:
                    pct = int(status.progress() * 100)
                    if pct - last_logged_pct >= 20 or pct == 100:
                        last_logged_pct = pct
                        elapsed = time_mod.time() - start_time
                        speed_mb = (os_mod.path.getsize(video_path) * status.progress() / (1024 * 1024)) / (elapsed if elapsed > 0 else 1)
                        logging.info(f"   └─ 上傳進度: {pct}% ({speed_mb:.1f} MB/s)")
                        sys.stdout.flush()
        except Exception as e:
            err_str = str(e)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e, upload=True) is True:
                logging.info("🔄 配額已切換至下一專案，重新發起本次影片上傳作業...")
                continue

            paused = classify_daily_limit(e)
            if "uploadLimitExceeded" in err_str:
                logging.error("🚨 【YouTube 頻道每日影片上傳數量限制】 (uploadLimitExceeded)")
                logging.error("👉 您的 YouTube 頻道今日上傳影片數量已達上限（此為 YouTube 頻道安全防範限制，與 API 配額無關）。")
                logging.error("👉 請等待 24 小時後重試，或前往 YouTube Studio 開通「手機號碼驗證 / 高級功能」以提升每日上傳上限！")
            elif "quotaExceeded" in err_str or "dailyLimitExceeded" in err_str:
                logging.error("🚨 【YouTube API 每日配額用盡】 (quotaExceeded)")
                logging.error("👉 所有專案配額皆已用盡；將在太平洋時間午夜重置後自動重試。")
            else:
                logging.error(f"❌ 影片上傳失敗: {e}")
            if paused:
                raise paused from e
            raise

        video_id = response.get("id")
        logging.info(f"✅ 上傳成功！影片 ID: {video_id} (網址: https://www.youtube.com/watch?v={video_id})")
        sys.stdout.flush()

        return video_id


def upload_caption_file(youtube, video_id, srt_path, language="zh-TW", name="繁體中文",
                        visibility_attempts=5, initial_visibility_delay=2):
    """使用 YouTube Captions API 上傳 CC 字幕軌檔 (SRT)"""
    if not srt_path or not os.path.exists(srt_path) or os.path.getsize(srt_path) < 10:
        logging.warning(f"⚠️ [Caption] 字幕檔不存在或為空: {srt_path}")
        return False

    media_file_cls = _get_symbol("MediaFileUpload", MediaFileUpload)
    time_mod = _get_symbol("time", time)
    visibility_failures = 0

    while True:
        # 1. 清除該影片歷史舊字幕軌 (避免舊測試檔殘留)
        try:
            cap_list = youtube.captions().list(part="snippet", videoId=video_id).execute()
            for item in cap_list.get("items", []):
                cap_id = item["id"]
                logging.info(f"  🧹 清除舊字幕軌 (ID: {cap_id})...")
                try:
                    youtube.captions().delete(id=cap_id).execute()
                except Exception as e:
                    logging.warning(f"  無法刪除舊字幕軌 {cap_id}: {e}")
        except Exception as e:
            err_str = str(e)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新發起 CC 字幕清理與上傳...")
                continue
            if "videoNotFound" in err_str:
                visibility_failures += 1
                if visibility_failures < visibility_attempts:
                    delay = min(initial_visibility_delay * (2 ** (visibility_failures - 1)), 16)
                    logging.warning(
                        "影片尚未對新 API 專案可見；%s 秒後重試字幕上傳 (%s/%s)。",
                        delay, visibility_failures, visibility_attempts,
                    )
                    time_mod.sleep(delay)
                    continue
            logging.warning(f"  無法列出既有字幕軌: {e}")

        # 2. 上傳全新 SRT 字幕檔
        file_size_kb = os.path.getsize(srt_path) / 1024.0
        logging.info(f"💬 開始 API 上傳 CC 字幕軌至 [Video ID: {video_id}] ({file_size_kb:.1f} KB)...")

        body = {
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": False
            }
        }
        media = media_file_cls(srt_path, mimetype="*/*", resumable=False)

        try:
            req = youtube.captions().insert(part="snippet", body=body, media_body=media)
            res = req.execute()
            cap_id = res.get("id")
            logging.info(f"🎉 CC 字幕成功上傳並生效！(Video ID: {video_id}, Caption ID: {cap_id})")
            return True
        except Exception as e:
            err_str = str(e)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(e) is True:
                logging.info("🔄 配額已切換至下一專案，重新嘗試上傳 CC 字幕...")
                continue
            if "videoNotFound" in err_str:
                visibility_failures += 1
                if visibility_failures < visibility_attempts:
                    delay = min(initial_visibility_delay * (2 ** (visibility_failures - 1)), 16)
                    logging.warning(
                        "影片尚未對新 API 專案可見；%s 秒後重試字幕上傳 (%s/%s)。",
                        delay, visibility_failures, visibility_attempts,
                    )
                    time_mod.sleep(delay)
                    continue
                logging.error(f"❌ 影片 [Video ID: {video_id}] 在 YouTube 上不存在 (videoNotFound)！")
                raise VideoNotFoundError(f"Video {video_id} not found on YouTube", video_id=video_id, original_error=e) from e
            logging.error(f"❌ 上傳 CC 字幕失敗 [Video ID: {video_id}]: {e}")
            paused = classify_daily_limit(e)
            if paused:
                raise paused from e
            return False


def set_video_privacy(youtube, video_id, privacy_status):
    """Publish only after every required post-upload action succeeds."""
    while True:
        try:
            youtube.videos().update(
                part="status",
                body={"id": video_id, "status": {"privacyStatus": privacy_status}},
            ).execute()
            logging.info("Video %s privacy changed to %s", video_id, privacy_status)
            return True
        except Exception as error:
            err_str = str(error)
            if ("quotaExceeded" in err_str or "dailyLimitExceeded" in err_str) and callable(getattr(youtube, "rotate_on_quota", None)) and youtube.rotate_on_quota(error) is True:
                logging.info("🔄 配額已切換至下一專案，重新更新影片隱私狀態...")
                continue
            if "videoNotFound" in err_str:
                raise VideoNotFoundError(f"Video {video_id} not found on YouTube", video_id=video_id, original_error=error) from error
            logging.error("Failed to change video %s privacy to %s: %s", video_id, privacy_status, error)
            paused = classify_daily_limit(error)
            if paused:
                raise paused from error
            return False


def is_valid_chinese_caption(snippet):
    """Return whether a caption snippet represents a valid Chinese caption track."""
    if not isinstance(snippet, dict):
        return False
    lang = str(snippet.get("language") or "").strip().lower()
    return lang in {"zh-tw", "zh-hant", "zh-hk", "zh", "cmn"}


def resolve_part_srt(title="", part_num=None, search_dirs=None):
    """Locate merged part SRT file by part number or chapter range."""
    if search_dirs is None:
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        search_dirs = [
            os.path.join(BASE_DIR, "Upload_Subtitles"),
            os.path.join(BASE_DIR, "prepared_parts"),
            os.path.join(BASE_DIR, "Output"),
        ]

    for s_dir in search_dirs:
        if not os.path.exists(s_dir):
            continue
        for root, _, files in os.walk(s_dir):
            for file in files:
                if not file.endswith(".srt"):
                    continue
                full_path = os.path.join(root, file)
                if part_num is not None:
                    m = re.search(r"Part[_\s]?0*(\d+)", file, re.IGNORECASE)
                    if m and int(m.group(1)) == int(part_num):
                        return full_path
                if title:
                    m_title = re.search(r"(\d+)~(\d+)", title)
                    if m_title:
                        s_c, e_c = m_title.group(1), m_title.group(2)
                        if f"Ch{s_c}_to_Ch{e_c}" in file or f"ch{s_c}_to_ch{e_c}" in file:
                            return full_path
    return None


def resolve_part_cover(title="", part_num=None, search_dirs=None):
    """Locate merged part cover image by part number or title."""
    if search_dirs is None:
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        search_dirs = [
            os.path.join(BASE_DIR, "Upload_Cover"),
            os.path.join(BASE_DIR, "prepared_parts"),
            os.path.join(BASE_DIR, "Cover"),
            os.path.join(BASE_DIR, "Output"),
        ]

    for s_dir in search_dirs:
        if not os.path.exists(s_dir):
            continue
        for root, _, files in os.walk(s_dir):
            for file in files:
                if not file.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    continue
                full_path = os.path.join(root, file)
                if part_num is not None:
                    m = re.search(r"Part[_\s]?0*(\d+)", file, re.IGNORECASE)
                    if m and int(m.group(1)) == int(part_num):
                        return full_path
                if title:
                    m_title = re.search(r"(\d+)~(\d+)", title)
                    if m_title:
                        s_c, e_c = m_title.group(1), m_title.group(2)
                        if f"Ch{s_c}_to_Ch{e_c}" in file or f"ch{s_c}_to_ch{e_c}" in file:
                            return full_path
    return None


def generate_part_srt(sliced_items, output_srt_path):
    """從 sliced_items 中的單章 srt 檔與 dur，無縫動態合併生成整個 Part 的完整 SRT"""
    try:
        from ..subtitle_gen import parse_timestamp_to_seconds, format_timestamp
    except ImportError:
        from subtitle_gen import parse_timestamp_to_seconds, format_timestamp

    current_offset = 0.0
    global_index = 1
    total_blocks = 0

    with open(output_srt_path, "w", encoding="utf-8") as out_f:
        for item in sliced_items:
            srt_p = item.get("srt_path")
            dur = item.get("dur", 0.0)

            if srt_p and os.path.exists(srt_p):
                try:
                    with open(srt_p, "r", encoding="utf-8") as in_f:
                        content = re.split(r'\n\s*\n', in_f.read().strip())
                        for block in content:
                            lines = block.strip().split('\n')
                            if len(lines) >= 3:
                                time_line = lines[1]
                                if '-->' in time_line:
                                    start_str, end_str = time_line.split('-->')
                                    start_sec = parse_timestamp_to_seconds(start_str.strip())
                                    end_sec = parse_timestamp_to_seconds(end_str.strip())

                                    new_start_ts = format_timestamp(start_sec + current_offset)
                                    new_end_ts = format_timestamp(end_sec + current_offset)

                                    out_f.write(f"{global_index}\n")
                                    out_f.write(f"{new_start_ts} --> {new_end_ts}\n")
                                    for text_line in lines[2:]:
                                        out_f.write(f"{text_line}\n")
                                    out_f.write("\n")
                                    global_index += 1
                                    total_blocks += 1
                except Exception as e:
                    logging.warning(f"解析字幕檔失敗 {srt_p}: {e}")
            current_offset += dur
    logging.info(f"✅ 已生成 Part 合併字幕檔: {os.path.basename(output_srt_path)} (共 {total_blocks} 條字幕)")
    return total_blocks > 0
