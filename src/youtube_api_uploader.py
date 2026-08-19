"""
youtube_api_uploader.py — YouTube Data API v3 暴速影片上傳 + 自動播放清單建置工具
"""

import os
import sys
import glob
import re
import socket
import ssl
import time
import shutil
import argparse
import logging
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, HttpError

try:
    from .part_builder import parse_chapter_num, get_media_duration, merge_part_videos
    from .publication_checkpoint import PART_STEPS, PublicationCheckpoint
    from .artifact_validation import validate_image, validate_srt, validate_video
    from .source_status import confirmed_missing_from_directory
    from .huggingface_archiver import HuggingFaceArchiver
except ImportError:
    # Support running this file directly as ``python src/youtube_api_uploader.py``.
    from part_builder import parse_chapter_num, get_media_duration, merge_part_videos
    from publication_checkpoint import PART_STEPS, PublicationCheckpoint
    from artifact_validation import validate_image, validate_srt, validate_video
    from source_status import confirmed_missing_from_directory
    from huggingface_archiver import HuggingFaceArchiver

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [YouTube-API] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

EXIT_RETRY_LATER = 75


class UploadPaused(RuntimeError):
    """A daily YouTube limit was reached; the upload can safely resume later."""

    def __init__(self, reason, retry_at, original_error=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at
        self.original_error = original_error


class ThumbnailUploadPaused(UploadPaused):
    """The video exists, but its custom thumbnail still needs to be applied."""

    def __init__(self, video_id, retry_at, original_error=None):
        super().__init__("thumbnailRateLimit", retry_at, original_error)
        self.video_id = video_id


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def save_resume_state(path, run_id, privacy, status, reason="", retry_at=None,
                      completed_titles=None, part_plan=None, pending_thumbnails=None,
                      playlist_url=None, pending_playlist=None,
                      pending_captions=None, pending_publish=None):
    data = {
        "version": 4,
        "run_id": str(run_id) if run_id else "",
        "privacy": privacy,
        "status": status,
        "reason": reason,
        "retry_at": retry_at.isoformat() if retry_at else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "completed_titles": sorted(completed_titles or []),
        # Once a Part boundary is chosen it is immutable.  This is what makes a
        # resumed run continue 1-50, 51-100 instead of repartitioning 1-70, ...
        "part_plan": list(part_plan or []),
        # title -> video id.  A video upload is durable even when the following
        # thumbnails.set call is rate-limited, so resume must repair it instead
        # of uploading the same multi-hour video a second time.
        "pending_thumbnails": dict(pending_thumbnails or {}),
        # title -> video id. A title is not complete until playlistItems.insert
        # succeeds. Persisting the id prevents an uploaded orphan on resume.
        "pending_playlist": dict(pending_playlist or {}),
        "pending_captions": dict(pending_captions or {}),
        "pending_publish": dict(pending_publish or {}),
        "playlist_url": playlist_url,
    }
    _atomic_write_json(path, data)
    logging.info("💾 上傳斷點已儲存：%s (%s)", path, status)


def load_resume_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def recover_completed_titles_from_playlist(completed_titles, existing_titles, planned_titles):
    """Rebuild progress from exact planned-title matches in the target playlist."""
    recovered = set(planned_titles) & set(existing_titles)
    completed_titles.update(recovered)
    return recovered


def part_number_for_title(part_plan, title):
    planned = next((part for part in part_plan if str(part.get("title") or "") == str(title)), None)
    return int(planned["part_num"]) if planned else None


def classify_daily_limit(error):
    """Return an UploadPaused instance for the two retryable daily limits."""
    text = str(error)
    now = datetime.now(timezone.utc)
    if "uploadLimitExceeded" in text:
        # This is a rolling channel limit, not the API project's midnight quota.
        return UploadPaused("uploadLimitExceeded", now + timedelta(hours=24, minutes=15), error)
    if "quotaExceeded" in text or "dailyLimitExceeded" in text:
        pacific = ZoneInfo("America/Los_Angeles")
        local_now = now.astimezone(pacific)
        next_midnight = (local_now + timedelta(days=1)).replace(
            hour=0, minute=15, second=0, microsecond=0
        )
        return UploadPaused("quotaExceeded", next_midnight.astimezone(timezone.utc), error)
    return None


def is_transient_upload_error(error):
    """Return whether a resumable upload should retry the same session."""
    if isinstance(error, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(error, HttpError):
        return getattr(getattr(error, "resp", None), "status", None) in {
            429, 500, 502, 503, 504,
        }
    # httplib2 wraps several socket/TLS failures as OSError/IOError. This is
    # evaluated only around next_chunk(), so retrying is safe and bounded.
    return isinstance(error, OSError)

def get_authenticated_service():
    """獲取與授權 YouTube API v3 Service (支援 client_secret.json / token.json / env refresh token)"""
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token_path = os.path.join(BASE_DIR, "token.json")
    client_secret_path = os.path.join(BASE_DIR, "client_secret.json")

    creds = None

    # 1. 嘗試從 token.json 讀取
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path)
        except Exception as e:
            logging.warning(f"無法讀取 token.json: {e}")

    # 2. 嘗試從環境變數 (YOUTUBE_REFRESH_TOKEN, YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET) 讀取
    ref_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()

    if (not creds or not creds.valid) and ref_token and client_id and client_secret:
        creds = Credentials(
            token=None,
            refresh_token=ref_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=None
        )

    # 3. 嘗試動態合成 client_secret.json (如果 CI/CD 或環境中不存在)
    if not os.path.exists(client_secret_path) and client_id and client_secret:
        try:
            import json
            cs_data = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token"
                }
            }
            with open(client_secret_path, "w", encoding="utf-8") as f:
                json.dump(cs_data, f)
            logging.info(f"✅ 已由環境變數動態生成 {client_secret_path}")
        except Exception as e:
            logging.warning(f"無法寫入 client_secret.json: {e}")

    # 4. 重新整理 Token
    if creds and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
        except Exception as e:
            logging.warning(f"重新整理 Refresh Token 失敗: {e}")
            if "invalid_scope" in str(e):
                logging.info("🔄 嘗試清除顯式 Scope 重新刷新憑證...")
                try:
                    creds._scopes = None
                    creds.refresh(Request())
                    with open(token_path, "w", encoding="utf-8") as token_file:
                        token_file.write(creds.to_json())
                    logging.info("✅ 成功使用 Refresh Token 原始授權刷新 Access Token！")
                except Exception as ex2:
                    logging.error(f"❌ 再次刷新失敗: {ex2}")
                    creds = None
            else:
                creds = None

    # 5. 如果沒有有效憑證，判斷是否為 CI/CD 無頭環境
    if not creds or not creds.valid:
        is_ci = os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")
        if is_ci:
            logging.error("❌ GitHub Actions 無頭環境無法開啟瀏覽器進行互動式授權！")
            logging.error("👉 請檢查 GitHub Secrets 中的 YOUTUBE_REFRESH_TOKEN / YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET 是否正確。")
            logging.error("👉 若 Refresh Token 已失效，請在本地 GUI 重新進行 YouTube 授權並將新 Token 更新至 GitHub Secrets。")
            sys.exit(1)

        if not os.path.exists(client_secret_path):
            logging.error(f"❌ 找不到 client_secret.json 且未設定 YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET！請在 GitHub Secrets 設定憑證。")
            sys.exit(1)

        try:
            logging.info("🔑 正在開啟瀏覽器進行 YouTube OAuth2 登入授權...")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)

            with open(token_path, "w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
            logging.info(f"✅ 授權成功！憑證已儲存至 {token_path}")
        except Exception as e:
            logging.error(f"❌ 無法完成 YouTube API 授權: {e}")
            sys.exit(1)

    return build("youtube", "v3", credentials=creds)

def parse_chapter_info(filename):
    """從檔名解析起始與結束章節號碼，供升序排序"""
    m_range = re.search(r'chapter_(\d+)_to_(\d+)', filename, re.IGNORECASE)
    if m_range:
        return int(m_range.group(1)), int(m_range.group(2))
    
    m_single = re.search(r'chapter_(\d+)', filename, re.IGNORECASE)
    if m_single:
        chap = int(m_single.group(1))
        return chap, chap
        
    m_worker = re.search(r'worker-(\d+)', filename, re.IGNORECASE)
    if m_worker:
        w_id = int(m_worker.group(1))
        return w_id * 120 + 1, (w_id + 1) * 120

    return 999999, 999999

def get_or_create_playlist(youtube, playlist_title, playlist_desc=""):
    """Return ``(playlist_id, created)`` for the requested playlist."""
    logging.info(f"🔍 檢查 YouTube 頻道是否存在播放清單:【{playlist_title}】...")
    try:
        request = youtube.playlists().list(
            part="snippet,status",
            mine=True,
            maxResults=50
        )
        response = request.execute()

        for item in response.get("items", []):
            if item["snippet"]["title"].strip() == playlist_title.strip():
                playlist_id = item["id"]
                logging.info(f"✅ 找到已有播放清單 (ID: {playlist_id}):【{playlist_title}】")
                return playlist_id, False

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
        logging.error(f"❌ 查詢/建立播放清單失敗: {e}")
        paused = classify_daily_limit(e)
        if paused:
            raise paused from e
        return None, False

def add_video_to_playlist(youtube, playlist_id, video_id, position=None):
    """將影片加到指定的播放清單中 (依呼叫順序追加)"""
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
        youtube.playlistItems().insert(
            part="snippet",
            body=body
        ).execute()
        logging.info(f"📋 成功將影片 [Video ID: {video_id}] 加入播放清單！")
        return True
    except Exception as e:
        logging.warning(f"⚠️ 將影片 [Video ID: {video_id}] 加入播放清單失敗: {e}")
        return False

def get_existing_playlist_video_titles(youtube, playlist_id):
    """獲取播放清單中已存在的影片標題集合，實現斷點續傳與自動跳過"""
    if not playlist_id:
        return set()
    titles = set()
    try:
        next_page_token = None
        while True:
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
        logging.info(f"📋 成功獲取播放清單已有 {len(titles)} 部影片，開啟【智能斷點續傳】跳過機制！")
    except Exception as e:
        if "playlistNotFound" in str(e):
            logging.info("📋 播放清單全新建立完成，目前為空狀態。")
        else:
            logging.warning(f"無法獲取播放清單既有影片清單: {e}")
    return titles


def get_playlist_video_index(youtube, playlist_id, attempts=5, initial_delay=2):
    """Return playlist title -> video id for checkpoint migration/repair."""
    index = {}
    if not playlist_id:
        return index
    next_page_token = None
    while True:
        for attempt in range(attempts):
            try:
                res = youtube.playlistItems().list(
                    part="snippet", playlistId=playlist_id, maxResults=50,
                    pageToken=next_page_token,
                ).execute()
                break
            except HttpError as error:
                is_not_found = (
                    getattr(getattr(error, "resp", None), "status", None) == 404
                    or "playlistNotFound" in str(error)
                )
                if not is_not_found or attempt == attempts - 1:
                    raise
                delay = initial_delay * (2 ** attempt)
                logging.warning(
                    "播放清單尚未同步完成；%s 秒後重試 (%s/%s)。",
                    delay, attempt + 1, attempts,
                )
                time.sleep(delay)
        for item in res.get("items", []):
            snippet = item.get("snippet", {})
            video_id = snippet.get("resourceId", {}).get("videoId")
            title = str(snippet.get("title") or "").strip()
            if title and video_id:
                index[title] = video_id
        next_page_token = res.get("nextPageToken")
        if not next_page_token:
            return index


def get_channel_upload_video_index(youtube):
    """Return title -> video id for the authenticated channel's uploads."""
    channels = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = channels.get("items", [])
    if not items:
        return {}
    uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    return get_playlist_video_index(youtube, uploads_id)

def set_video_thumbnail(youtube, video_id, cover_path, attempts=5):
    """Apply a custom thumbnail or raise a resumable post-upload pause."""
    if not cover_path or not os.path.exists(cover_path):
        raise ThumbnailUploadPaused(
            video_id, datetime.now(timezone.utc) + timedelta(hours=2),
            RuntimeError(f"封面檔不存在：{cover_path}"),
        )

    last_error = None
    for attempt in range(attempts):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(cover_path)
            ).execute()
            logging.info("🖼️ 成功更新影片封面縮圖！")
            return True
        except Exception as e:
            last_error = e
            if "uploadRateLimitExceeded" in str(e) or "429" in str(e):
                wait_sec = (attempt + 1) * 10
                logging.warning(
                    "⚠️ 設定縮圖觸發速率限制 (429)，等待 %s 秒後重試 (%s/%s)...",
                    wait_sec, attempt + 1, attempts,
                )
                time.sleep(wait_sec)
                continue
            raise ThumbnailUploadPaused(
                video_id, datetime.now(timezone.utc) + timedelta(hours=2), e,
            ) from e

    raise ThumbnailUploadPaused(
        video_id,
        datetime.now(timezone.utc) + timedelta(hours=2),
        last_error,
    )


def upload_video_file(youtube, video_path, title, description, category_id="22",
                      privacy_status="public", cover_path=None,
                      network_attempts=8, initial_retry_delay=2):
    """使用 Resumable 上傳 MP4 到 YouTube"""
    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    logging.info(f"📤 開始 API 極速上傳影片: {title} (檔案大小: {file_size_mb:.1f} MB)...")
    sys.stdout.flush()

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "categoryId": category_id
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(video_path, chunksize=10*1024*1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    last_logged_pct = -10
    start_time = time.time()
    consecutive_network_failures = 0

    try:
        while response is None:
            try:
                # Let googleapiclient retry short failures internally first.
                # The outer loop preserves and resumes the same upload session
                # when TLS/socket failures outlive those internal attempts.
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
                time.sleep(delay)
                continue
            if status:
                pct = int(status.progress() * 100)
                if pct - last_logged_pct >= 20 or pct == 100:
                    last_logged_pct = pct
                    elapsed = time.time() - start_time
                    speed_mb = (os.path.getsize(video_path) * status.progress() / (1024 * 1024)) / (elapsed if elapsed > 0 else 1)
                    logging.info(f"   └─ 上傳進度: {pct}% ({speed_mb:.1f} MB/s)")
                    sys.stdout.flush()
    except Exception as e:
        err_str = str(e)
        paused = classify_daily_limit(e)
        if "uploadLimitExceeded" in err_str:
            logging.error("🚨 【YouTube 頻道每日影片上傳數量限制】 (uploadLimitExceeded)")
            logging.error("👉 您的 YouTube 頻道今日上傳影片數量已達上限（此為 YouTube 頻道安全防範限制，與 API 配額無關）。")
            logging.error("👉 請等待 24 小時後重試，或前往 YouTube Studio 開通「手機號碼驗證 / 高級功能」以提升每日上傳上限！")
        elif "quotaExceeded" in err_str or "dailyLimitExceeded" in err_str:
            logging.error("🚨 【YouTube API 每日配額用盡】 (quotaExceeded)")
            logging.error("👉 YouTube Data API 配額會在太平洋時間午夜重置；程式已依 PDT/PST 自動換算並預留 15 分鐘。")
        else:
            logging.error(f"❌ 影片上傳失敗: {e}")
        if paused:
            raise paused from e
        raise

    video_id = response.get("id")
    logging.info(f"✅ 上傳成功！影片 ID: {video_id} (網址: https://www.youtube.com/watch?v={video_id})")
    sys.stdout.flush()

    set_video_thumbnail(youtube, video_id, cover_path)

    return video_id

def upload_caption_file(youtube, video_id, srt_path, language="zh-TW", name="繁體中文"):
    """使用 YouTube Captions API 上傳 CC 字幕軌檔 (SRT)"""
    if not srt_path or not os.path.exists(srt_path) or os.path.getsize(srt_path) < 10:
        logging.warning(f"⚠️ [Caption] 字幕檔不存在或為空: {srt_path}")
        return False

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
    media = MediaFileUpload(srt_path, mimetype="*/*", resumable=False)

    try:
        req = youtube.captions().insert(part="snippet", body=body, media_body=media)
        res = req.execute()
        cap_id = res.get("id")
        logging.info(f"🎉 CC 字幕成功上傳並生效！(Video ID: {video_id}, Caption ID: {cap_id})")
        return True
    except Exception as e:
        logging.error(f"❌ 上傳 CC 字幕失敗 [Video ID: {video_id}]: {e}")
        paused = classify_daily_limit(e)
        if paused:
            raise paused from e
        return False


def set_video_privacy(youtube, video_id, privacy_status):
    """Publish only after every required post-upload action succeeds."""
    try:
        youtube.videos().update(
            part="status",
            body={"id": video_id, "status": {"privacyStatus": privacy_status}},
        ).execute()
        logging.info("Video %s privacy changed to %s", video_id, privacy_status)
        return True
    except Exception as error:
        logging.error("Failed to change video %s privacy to %s: %s", video_id, privacy_status, error)
        return False


def verify_published_part(youtube, video_id, playlist_id, privacy_status, attempts=5):
    """Read YouTube back after writes; API success alone is not final acceptance."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = youtube.videos().list(part="status,snippet", id=video_id).execute()
            items = response.get("items") or []
            if not items:
                raise RuntimeError(f"uploaded video cannot be read back: {video_id}")
            actual_privacy = (items[0].get("status") or {}).get("privacyStatus")
            if actual_privacy != privacy_status:
                raise RuntimeError(f"privacy mismatch: expected {privacy_status}, got {actual_privacy}")
            thumbnails = (items[0].get("snippet") or {}).get("thumbnails") or {}
            if not thumbnails:
                raise RuntimeError("video thumbnail cannot be read back")
            captions = youtube.captions().list(part="id,snippet", videoId=video_id).execute().get("items") or []
            matching_captions = [
                item.get("snippet") or {}
                for item in captions
                if (item.get("snippet") or {}).get("language") == "zh-TW"
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
            playlist_index = get_playlist_video_index(youtube, playlist_id)
            if video_id not in set(playlist_index.values()):
                raise RuntimeError("video cannot be read back from the target playlist")
            return {"youtube_video_id": video_id, "privacy": actual_privacy, "caption_language": "zh-TW"}
        except Exception as error:
            last_error = error
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 16))
    raise RuntimeError(f"final YouTube read-back validation failed: {last_error}")

def generate_part_srt(sliced_items, output_srt_path):
    """從 sliced_items 中的單章 srt 檔與 dur，無縫動態合併生成整個 Part 的完整 SRT"""
    try:
        from .subtitle_gen import parse_timestamp_to_seconds, format_timestamp
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
                        content = in_f.read().strip().split('\n\n')
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

def get_run_artifact_names(run_id, repo):
    # One worker normally publishes both mp4-worker-* and video-worker-*.
    # GitHub returns only 30 artifacts per page by default, so a 20-worker run
    # already spans multiple pages (shared-config + 40 worker artifacts).
    cmd = [
        "gh", "api", "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        "--jq", ".artifacts[].name",
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"Failed to fetch artifacts for run {run_id}: {res.stderr}")
        return []
    all_names = [n.strip() for n in res.stdout.splitlines() if n.strip()]
    return select_worker_artifacts(all_names)


def select_worker_artifacts(all_names):
    """Select one artifact per worker, preferring the lightweight MP4 artifact."""
    selected = {}
    for prefix in ("video-worker-", "mp4-worker-"):
        for name in all_names:
            if name.startswith(prefix):
                selected[artifact_worker_index(name)] = name
    return [selected[index] for index in sorted(selected)]


def artifact_worker_index(name):
    """Extract the worker id, never the ``4`` embedded in the ``mp4`` prefix."""
    match = re.search(r"(?:mp4|video)-worker-(\d+)$", name)
    if not match:
        raise ValueError(f"無法識別 Worker Artifact 名稱：{name}")
    return int(match.group(1))


def scan_artifact_chapters(artifact_dir, artifact_name):
    """Inventory chapter media in one downloaded artifact."""
    chapters = []
    for video_path in glob.glob(os.path.join(artifact_dir, "**", "*.mp4"), recursive=True):
        chapter_num = parse_chapter_num(os.path.basename(video_path))
        if chapter_num == 999999:
            continue
        srt_path = video_path.replace("/Video/", "/Subtitles/").replace("\\Video\\", "\\Subtitles\\").replace(".mp4", ".srt")
        if not os.path.exists(srt_path):
            srt_matches = glob.glob(
                os.path.join(artifact_dir, "**", f"*chapter_{chapter_num}.srt"),
                recursive=True,
            )
            srt_path = srt_matches[0] if srt_matches else None
        chapters.append({
            "artifact": artifact_name,
            "chap_num": chapter_num,
            "dur": get_media_duration(video_path),
            "video_relpath": os.path.relpath(video_path, artifact_dir),
            "srt_relpath": os.path.relpath(srt_path, artifact_dir) if srt_path else None,
        })
    return sorted(chapters, key=lambda item: item["chap_num"])


def _validate_complete_chapter_inventory(chapters, expected_start, expected_end):
    """Refuse to upload unless the complete book is present exactly once."""
    numbers = [int(item["chap_num"]) for item in chapters]
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    expected = list(range(int(expected_start), int(expected_end) + 1))
    missing = sorted(set(expected) - set(numbers))
    unexpected = sorted(set(numbers) - set(expected))
    if duplicates or missing or unexpected or numbers != sorted(numbers):
        raise RuntimeError(
            "章節盤點不完整，禁止開始上傳："
            f"重複={duplicates[:10]}，缺少={missing[:10]}，超出範圍={unexpected[:10]}"
        )


def validate_chapter_inventory(chapters, expected_start, expected_end, confirmed_missing=None):
    """Accept absent chapters only when worker artifacts prove origin omission."""
    confirmed_missing = {int(value) for value in (confirmed_missing or set())}
    numbers = [int(item["chap_num"]) for item in chapters]
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    expected = set(range(int(expected_start), int(expected_end) + 1))
    missing = sorted(expected - set(numbers))
    unresolved = sorted(set(missing) - confirmed_missing)
    unexpected = sorted(set(numbers) - expected)
    if duplicates or unresolved or unexpected or numbers != sorted(numbers):
        raise RuntimeError(
            "chapter inventory is not publishable: "
            f"duplicates={duplicates[:10]}, unresolved_missing={unresolved[:10]}, "
            f"unexpected={unexpected[:10]}"
        )
    return {"missing": missing, "confirmed_missing": sorted(set(missing) & confirmed_missing)}


def build_part_plan_from_inventory(chapters, min_seconds=10 * 3600, max_seconds=11 * 3600,
                                   confirmed_missing=None):
    """Plan every Part globally before any merge or YouTube API upload."""
    if not chapters:
        raise RuntimeError("沒有可供分部的章節")
    ordered = sorted(chapters, key=lambda item: int(item["chap_num"]))
    plan = []
    current = []
    current_duration = 0.0
    for item in ordered:
        duration = float(item["dur"])
        if duration <= 0:
            raise RuntimeError(f"第 {item['chap_num']} 章無法取得有效片長")
        if current and current_duration + duration > max_seconds:
            plan.append(_make_planned_part(len(plan) + 1, current, current_duration))
            current = []
            current_duration = 0.0
        current.append(item)
        current_duration += duration
    if current:
        plan.append(_make_planned_part(len(plan) + 1, current, current_duration))

    confirmed_missing = {int(value) for value in (confirmed_missing or set())}
    for previous, following in zip(plan, plan[1:]):
        gap = set(range(previous["end_chap"] + 1, following["start_chap"]))
        if gap and not gap.issubset(confirmed_missing):
            raise RuntimeError(f"分部不連續：Part {previous['part_num']} 後接 Part {following['part_num']}")
    unassigned_missing = set(confirmed_missing)
    for part in plan:
        assigned = {chapter for chapter in unassigned_missing if chapter <= part["end_chap"]}
        part["source_missing_chapters"] = sorted(assigned)
        unassigned_missing.difference_update(assigned)
    if plan and unassigned_missing:
        plan[-1]["source_missing_chapters"].extend(sorted(unassigned_missing))
    for part in plan[:-1]:
        if part["duration"] < min_seconds:
            logging.warning("Part %s 只有 %.2f 小時；受 11 小時硬上限約束。", part["part_num"], part["duration"] / 3600)
    return plan


def _make_planned_part(part_num, items, duration):
    return {
        "part_num": part_num,
        "start_chap": int(items[0]["chap_num"]),
        "end_chap": int(items[-1]["chap_num"]),
        "chapters": [int(item["chap_num"]) for item in items],
        "artifacts": list(dict.fromkeys(item["artifact"] for item in items)),
        "duration": duration,
    }

def download_artifact_task(run_id, repo, artifact_name, dest_dir):
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)
    
    dl_cmd = [
        "gh", "run", "download", str(run_id),
        "--repo", repo,
        "--name", artifact_name,
        "--dir", dest_dir
    ]
    res = subprocess.run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.returncode == 0

def get_latest_successful_run_id(repo):
    """Fallback when no run_id is passed and no state.json exists: auto-detect latest video production run."""
    try:
        cmd = [
            "gh", "api",
            f"repos/{repo}/actions/workflows/audiobook.yml/runs?status=success&per_page=1",
            "--jq", ".workflow_runs[0].id"
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout.strip() and res.stdout.strip() != "null":
            return res.stdout.strip()
    except Exception as e:
        logging.warning("無法自動查詢最新 Run ID: %s", e)
    return None

def main():
    parser = argparse.ArgumentParser(description="YouTube API Fast Uploader & Playlist Builder")
    parser.add_argument("--run-id", help="GitHub Actions Run ID containing video worker artifacts")
    parser.add_argument("--input-dir", help="Local directory containing MP4 files")
    parser.add_argument("--repo", default="hub-google/audiobook-generator", help="GitHub Repository")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"], help="Privacy status")
    parser.add_argument("--state-file", default="upload_resume_state/state.json",
                        help="Durable state restored/saved by GitHub Actions")
    parser.add_argument("--task-id", default=os.environ.get("QUEUE_TASK_ID", ""), help="Persistent cloud queue task ID")
    args = parser.parse_args()
    publication = PublicationCheckpoint(args.state_file)

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        raise RuntimeError("HF_TOKEN is required because the production archive is mandatory")
    hf_repo = os.environ.get("HF_ARCHIVE_REPO", "").strip()
    if not hf_repo:
        from huggingface_hub import HfApi
        hf_repo = f"{HfApi(token=hf_token).whoami()['name']}/audiobook-archive"
    hf_archiver = HuggingFaceArchiver(
        hf_repo, hf_token,
        os.path.join(os.path.dirname(os.path.abspath(args.state_file)), "hf_archive_state.json"),
    )
    hf_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf-archive")

    saved_state = load_resume_state(args.state_file)
    completed_titles = set()
    part_plan = []
    pending_thumbnails = {}
    pending_playlist = {}
    pending_captions = {}
    pending_publish = {}
    resume_state_matches = bool(
        saved_state
        and str(saved_state.get("run_id") or "")
        == str(args.run_id or saved_state.get("run_id") or "")
    )
    if resume_state_matches:
        completed_titles.update(saved_state.get("completed_titles") or [])
        part_plan = list(saved_state.get("part_plan") or [])
        pending_thumbnails = dict(saved_state.get("pending_thumbnails") or {})
        pending_playlist = dict(saved_state.get("pending_playlist") or {})
        pending_captions = dict(saved_state.get("pending_captions") or {})
        pending_publish = dict(saved_state.get("pending_publish") or {})
    valid_resume_statuses = {"paused", "running", "planned", "incomplete"}
    if resume_state_matches and saved_state.get("status") in valid_resume_statuses:
        if not args.run_id:
            args.run_id = str(saved_state.get("run_id") or "")
            args.privacy = saved_state.get("privacy") or args.privacy
        retry_text = saved_state.get("retry_at")
        if saved_state.get("status") == "paused" and retry_text:
            retry_at = datetime.fromisoformat(retry_text.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < retry_at:
                logging.info("⏳ 尚未到安全重試時間 %s；本次排程不會呼叫 YouTube。", retry_at.isoformat())
                return EXIT_RETRY_LATER

    if not args.run_id and not args.input_dir:
        logging.info("🔍 未指定 --run-id 且無有效斷點紀錄，嘗試自動查詢最新產檔成功的 Run ID...")
        latest_run_id = get_latest_successful_run_id(args.repo)
        if latest_run_id:
            args.run_id = latest_run_id
            logging.info("💡 自動鎖定最新產檔 Run ID: %s", args.run_id)
        else:
            logging.error("Strict success gate: no source run or local input was found")
            return 1

    if args.run_id:
        save_resume_state(args.state_file, args.run_id, args.privacy, "running",
                          completed_titles=completed_titles, part_plan=part_plan,
                          pending_thumbnails=pending_thumbnails,
                          pending_playlist=pending_playlist,
                          pending_captions=pending_captions,
                          pending_publish=pending_publish)

    youtube = get_authenticated_service()

    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, SRC_DIR)
    from metadata_gen import save_book_metadata

    book_title = "有聲小說全集"
    start_chap, end_chap = 1, 2400
    config_path = os.path.join(SRC_DIR, "..", "config.yaml")
    if args.run_id:
        # Use the source run's generated config. The repository copy may belong
        # to an older book and would create/cache a cover under the wrong title.
        shared_config_dir = os.path.abspath("temp_source_run_config")
        if download_artifact_task(args.run_id, args.repo, "shared-config", shared_config_dir):
            downloaded_config = os.path.join(shared_config_dir, "config.yaml")
            if os.path.exists(downloaded_config):
                config_path = downloaded_config
                logging.info("已載入來源 Run 的 shared-config：%s", config_path)
        else:
            raise RuntimeError(
                f"Strict success gate: source Run #{args.run_id} has no downloadable shared-config artifact"
            )
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if cfg:
                    book_title = cfg.get("book_title", book_title)
                    chaps = cfg.get("selected_indices", [])
                    if chaps:
                        start_chap = chaps[0]
                        end_chap = chaps[-1]
        except Exception as e:
            logging.warning(f"Could not load config.yaml: {e}")

    meta_info = save_book_metadata(book_title, start_chap, end_chap)
    cover_path = meta_info["cover_file"]
    if part_plan:
        publication.lock_plan(part_plan, run_id=args.run_id, book_title=book_title)

    playlist_name = f"《{book_title}》有聲小說全集"
    playlist_desc = f"《{book_title}》完整版有聲書全集 (第 {start_chap} 至 {end_chap} 章)，高音質連續播映版。\n歡迎訂閱開啟小鈴鐺！"
    publication.mark_global("playlist", "running")
    try:
        playlist_id, playlist_created = get_or_create_playlist(
            youtube, playlist_name, playlist_desc
        )
    except UploadPaused as paused:
        publication.mark_global("playlist", "paused", error=paused.reason)
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "paused",
            reason=paused.reason, retry_at=paused.retry_at,
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
        )
        logging.error(
            "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
            len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
            args.run_id, paused.reason,
        )
        return EXIT_RETRY_LATER
    if not playlist_id:
        publication.mark_global("playlist", "failed", error="playlistUnavailable")
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "failed",
            reason="playlistUnavailable", completed_titles=completed_titles,
            part_plan=part_plan, pending_thumbnails=pending_thumbnails,
        )
        logging.error("無法取得或建立 YouTube 播放清單，停止上傳。")
        return 1
    publication.mark_global("playlist", "completed", playlist_id=playlist_id)
    try:
        # A playlist returned by playlists.insert is necessarily empty. Avoid
        # querying playlistItems immediately while the new resource is still
        # propagating through YouTube's read path.
        existing_video_ids = (
            {} if playlist_created else get_playlist_video_index(youtube, playlist_id)
        )
    except HttpError as error:
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "failed",
            reason="playlistNotFound", completed_titles=completed_titles,
            part_plan=part_plan, pending_thumbnails=pending_thumbnails,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
        logging.error("播放清單重試後仍無法讀取：%s", error)
        return 1
    existing_titles = set(existing_video_ids)
    logging.info("📋 成功獲取播放清單已有 %s 部影片。", len(existing_titles))

    # Old checkpoints counted videos.insert as completion even when the later
    # playlist insertion failed. Recover those video ids from channel uploads.
    missing_from_playlist = completed_titles - existing_titles
    if missing_from_playlist:
        channel_uploads = get_channel_upload_video_index(youtube)
        for title in list(missing_from_playlist):
            video_id = channel_uploads.get(title)
            if not video_id:
                logging.error("Uploaded title is missing from the playlist and channel uploads: %s", title)
                continue
            pending_playlist[title] = video_id
            completed_titles.discard(title)
            logging.warning("Repairing orphan video playlist membership: %s (%s)", title, video_id)

    # Version-2 checkpoints predate pending_thumbnails.  The last completed
    # title is conservatively repaired once, which fixes the already uploaded
    # Part 11 whose five thumbnail attempts all received HTTP 429.
    if saved_state and int(saved_state.get("version") or 0) < 3 and completed_titles:
        for planned in reversed(part_plan):
            legacy_title = str(planned.get("title") or "").strip()
            if legacy_title in completed_titles and legacy_title in existing_video_ids:
                pending_thumbnails.setdefault(legacy_title, existing_video_ids[legacy_title])
                logging.info("🧭 舊版斷點遷移：將最後完成影片列入封面核對：%s", legacy_title)
                break

    # Finish durable post-upload work before creating any more videos.  This is
    # what repairs a thumbnails.set 429 on the next scheduled run.
    for pending_title, pending_video_id in list(pending_thumbnails.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        try:
            logging.info("🖼️ 續傳待補封面：%s (%s)", pending_title, pending_video_id)
            set_video_thumbnail(youtube, pending_video_id, cover_path)
            if pending_part_num:
                publication.complete(pending_part_num, "upload_video", youtube_video_id=pending_video_id)
                publication.complete(pending_part_num, "upload_thumbnail", youtube_video_id=pending_video_id)
        except ThumbnailUploadPaused as paused:
            if pending_part_num:
                publication.fail(pending_part_num, "upload_thumbnail", paused, paused=True, youtube_video_id=pending_video_id)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails, pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error(
                "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
                args.run_id, paused.reason,
            )
            return EXIT_RETRY_LATER
        del pending_thumbnails[pending_title]
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "running",
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
        )

    # CC subtitles are mandatory. The SRT directory is cached between runs so
    # a quota failure resumes this exact private video before any later upload.
    for pending_title, caption in list(pending_captions.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        video_id = caption.get("video_id")
        srt_path = caption.get("srt_path")
        try:
            caption_uploaded = upload_caption_file(youtube, video_id, srt_path)
        except UploadPaused as paused:
            if pending_part_num:
                publication.fail(pending_part_num, "upload_caption", paused, paused=True, youtube_video_id=video_id)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Caption quota exhausted; retry after %s", paused.retry_at.isoformat())
            return EXIT_RETRY_LATER
        if not caption_uploaded:
            if pending_part_num:
                publication.fail(pending_part_num, "upload_caption", RuntimeError("caption upload failed"), paused=True, youtube_video_id=video_id)
            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                "captionUploadFailed", retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            return EXIT_RETRY_LATER
        del pending_captions[pending_title]
        if pending_part_num:
            publication.complete(pending_part_num, "upload_caption", youtube_video_id=video_id)

    # Playlist membership is a required commit. Never upload a later Part while
    # an earlier uploaded video is still missing from the playlist.
    for pending_title, pending_video_id in list(pending_playlist.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        planned = next((p for p in part_plan if p.get("title") == pending_title), {})
        position = int(planned.get("part_num", 0)) - 1 if planned else None
        if not add_video_to_playlist(youtube, playlist_id, pending_video_id, position):
            if pending_part_num:
                publication.fail(pending_part_num, "add_playlist", RuntimeError("playlist insertion failed"), paused=True, youtube_video_id=pending_video_id)
            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                "playlistInsertFailed", retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Playlist insertion is mandatory; Part remains incomplete: %s", pending_title)
            return EXIT_RETRY_LATER
        del pending_playlist[pending_title]
        if pending_part_num:
            publication.complete(pending_part_num, "add_playlist", youtube_video_id=pending_video_id, position=position)
        pending_publish[pending_title] = pending_video_id
        existing_titles.add(pending_title)
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "running",
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )

    # Publishing is the final commit. Until this succeeds the video stays
    # private even if every other YouTube resource already exists.
    for pending_title, pending_video_id in list(pending_publish.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        if not set_video_privacy(youtube, pending_video_id, args.privacy):
            if pending_part_num:
                publication.fail(pending_part_num, "publish", RuntimeError("final publish failed"), paused=True, youtube_video_id=pending_video_id)
            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                "publishFailed", retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            return EXIT_RETRY_LATER
        del pending_publish[pending_title]
        completed_titles.add(pending_title)
        if pending_part_num:
            publication.complete(pending_part_num, "publish", youtube_video_id=pending_video_id, privacy=args.privacy)
            publication.mark(pending_part_num, "final_validation", "running")
            evidence = verify_published_part(youtube, pending_video_id, playlist_id, args.privacy)
            publication.complete(pending_part_num, "final_validation", **evidence)
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "running",
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )

    from metadata_gen import generate_video_title

    upload_subtitles_dir = os.path.abspath("Upload_Subtitles")
    os.makedirs(upload_subtitles_dir, exist_ok=True)
    temp_parts_dir = os.path.abspath("temp_parts_output")
    os.makedirs(temp_parts_dir, exist_ok=True)
    temp_dl_dir = os.path.abspath("temp_api_upload_workspace")
    os.makedirs(temp_dl_dir, exist_ok=True)

    part_counter = 1
    total_uploaded = 0

    if args.run_id:
        logging.info(f"📥 啟動【即時下載 ➔ 流水線打包 (10~11小時) ➔ 暴速上傳 ➔ 精準硬碟清理 ➔ 智能斷點續傳】模式...")
        logging.info(f"Target Run ID #{args.run_id}")

        artifact_names = get_run_artifact_names(args.run_id, args.repo)
        if not artifact_names:
            logging.error(f"❌ 未在 Run #{args.run_id} 中找到任何影片 Artifacts！")
            sys.exit(1)

        logging.info(f"共有 {len(artifact_names)} 個 Worker Artifacts 待處理。")

        min_seconds = 10.0 * 3600.0
        max_seconds = 11.0 * 3600.0

        # Phase 1: inventory every chapter before creating or uploading Part 1.
        # Probe one artifact at a time so the runner never stores the full book.
        inventory = []
        confirmed_source_missing = set()
        publication.mark_global("download_artifacts", "running")
        logging.info("🔎 第一階段：盤點全部 Worker Artifacts；此階段不會上傳任何影片。")
        for inventory_index, artifact_name in enumerate(artifact_names, 1):
            inventory_dir = os.path.join(temp_dl_dir, f"inventory-{artifact_name}")
            logging.info("📦 盤點 [%s/%s]：%s", inventory_index, len(artifact_names), artifact_name)
            if not download_artifact_task(args.run_id, args.repo, artifact_name, inventory_dir):
                raise RuntimeError(f"Artifact 下載失敗，禁止上傳：{artifact_name}")
            artifact_missing = confirmed_missing_from_directory(inventory_dir)
            confirmed_source_missing.update(artifact_missing)
            scanned = scan_artifact_chapters(inventory_dir, artifact_name)
            if not scanned and not artifact_missing:
                raise RuntimeError(f"Artifact 沒有章節 MP4，禁止上傳：{artifact_name}")
            inventory.extend(scanned)
            shutil.rmtree(inventory_dir, ignore_errors=True)

        publication.mark_global("download_artifacts", "completed", artifact_count=len(artifact_names))
        publication.mark_global("probe_durations", "completed", chapter_count=len(inventory))

        inventory.sort(key=lambda item: int(item["chap_num"]))
        publication.mark_global("validate_inventory", "running")
        inventory_result = validate_chapter_inventory(
            inventory, start_chap, end_chap, confirmed_source_missing
        )
        publication.mark_global(
            "validate_inventory", "completed", chapter_count=len(inventory),
            source_missing_chapters=inventory_result["confirmed_missing"],
        )
        candidate_plan = build_part_plan_from_inventory(
            inventory, min_seconds, max_seconds, confirmed_source_missing
        )
        for planned in candidate_plan:
            planned["title"] = generate_video_title(
                book_title,
                start_chap=planned["start_chap"],
                end_chap=planned["end_chap"],
                part_num=planned["part_num"],
            )
            logging.info(
                "🧭 Part %02d：第 %04d~%04d 章，共 %d 章，%.2f 小時",
                planned["part_num"], planned["start_chap"], planned["end_chap"],
                len(planned["chapters"]), planned["duration"] / 3600,
            )
        publication.lock_plan(candidate_plan, run_id=args.run_id, book_title=book_title)
        publication.mark_global("lock_plan", "completed", part_count=len(candidate_plan))
        part_plan = candidate_plan
        recovered_titles = recover_completed_titles_from_playlist(
            completed_titles,
            existing_titles,
            (planned["title"] for planned in part_plan),
        )
        if recovered_titles:
            logging.warning(
                "Recovered %s/%s completed Parts from exact title matches in the target playlist; "
                "they will not be uploaded again.",
                len(recovered_titles), len(part_plan),
            )
        leading_gap = set(range(start_chap, part_plan[0]["start_chap"]))
        if leading_gap and not leading_gap.issubset(confirmed_source_missing):
            raise RuntimeError("Part 1 並非從全書第一章開始，禁止上傳")
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "planned",
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
        logging.info("✅ 全書分部規劃已鎖定；第二階段將嚴格依 Part 1 → Part %s 串行上傳。", len(part_plan))

        chapter_pool = []

        import threading

        # 預先下載第 1 個 Artifact
        art_dir_first = os.path.join(temp_dl_dir, artifact_names[0])
        logging.info(f"📦 [1/{len(artifact_names)}] 下載 Artifact: {artifact_names[0]}...")
        download_artifact_task(args.run_id, args.repo, artifact_names[0], art_dir_first)

        prefetch_thread = None

        for a_idx, a_name in enumerate(artifact_names):
            art_dir = os.path.join(temp_dl_dir, a_name)

            # 🚀 立即發起【下一個 Artifact】的背景預下載線程 (雙線程流水線)
            if a_idx + 1 < len(artifact_names):
                next_name = artifact_names[a_idx + 1]
                next_dir = os.path.join(temp_dl_dir, next_name)
                logging.info(f"⚡ 【異步背景預載】發動背景線程預先下載下一個包 [{a_idx+2}/{len(artifact_names)}]: {next_name}...")
                prefetch_thread = threading.Thread(
                    target=download_artifact_task,
                    args=(args.run_id, args.repo, next_name, next_dir)
                )
                prefetch_thread.start()
            else:
                prefetch_thread = None

            m_files = glob.glob(os.path.join(art_dir, "**", "*.mp4"), recursive=True)
            for f_path in m_files:
                if not any(x["path"] == f_path for x in chapter_pool):
                    c_num = parse_chapter_num(os.path.basename(f_path))
                    dur = get_media_duration(f_path)

                    srt_path = f_path.replace("/Video/", "/Subtitles/").replace("\\Video\\", "\\Subtitles\\").replace(".mp4", ".srt")
                    if not os.path.exists(srt_path):
                        parent_art = os.path.dirname(os.path.dirname(f_path))
                        srt_matches = glob.glob(os.path.join(parent_art, "**", f"*chapter_{c_num}.srt"), recursive=True)
                        srt_path = srt_matches[0] if srt_matches else None

                    chapter_pool.append({
                        "path": f_path,
                        "srt_path": srt_path,
                        "chap_num": c_num,
                        "dur": dur
                    })

            chapter_pool.sort(key=lambda x: x["chap_num"])
            is_last_artifact = (a_idx == len(artifact_names) - 1)

            while True:
                if not chapter_pool:
                    break

                pool_dur = sum(x["dur"] for x in chapter_pool)

                # Wait until the pool exceeds 11 hours before closing a Part.
                # Otherwise artifact/worker size could change an identical book
                # from Ch1-50 to Ch1-70 on another run.
                if pool_dur <= max_seconds and not is_last_artifact:
                    logging.info(f"   目前累計 {len(chapter_pool)} 章 (約 {pool_dur/3600:.2f} 小時)，等待足夠章節以固定 11 小時邊界...")
                    break

                sliced_items = []
                sliced_dur = 0.0
                locked_part = next(
                    (p for p in part_plan if int(p.get("part_num", 0)) == part_counter),
                    None,
                )

                if locked_part:
                    locked_chapters = [int(n) for n in locked_part.get("chapters", [])]
                    available = {int(item["chap_num"]): item for item in chapter_pool}
                    missing = [n for n in locked_chapters if n not in available]
                    if missing and not is_last_artifact:
                        logging.info(
                            "鎖定的第 %s 部尚缺章節 %s，繼續下載下一個 Artifact。",
                            part_counter, missing[:8]
                        )
                        break
                    if missing:
                        raise RuntimeError(
                            f"斷點規劃損壞：第 {part_counter} 部缺少章節 {missing}"
                        )
                    sliced_items = [available[n] for n in locked_chapters]
                    sliced_dur = sum(item["dur"] for item in sliced_items)
                else:
                    for item in chapter_pool:
                        if sliced_items and (sliced_dur + item["dur"] > max_seconds):
                            break
                        sliced_items.append(item)
                        sliced_dur += item["dur"]
                        if sliced_dur >= max_seconds:
                            break

                if not sliced_items:
                    break

                s_c = sliced_items[0]["chap_num"]
                e_c = sliced_items[-1]["chap_num"]
                expected_title = generate_video_title(book_title, start_chap=s_c, end_chap=e_c, part_num=part_counter)

                if not locked_part:
                    locked_part = {
                        "part_num": part_counter,
                        "start_chap": s_c,
                        "end_chap": e_c,
                        "chapters": [int(item["chap_num"]) for item in sliced_items],
                        "title": expected_title,
                    }
                    part_plan.append(locked_part)
                    # Persist before merge/upload. A crash at any later instruction
                    # must reuse this exact chapter membership.
                    save_resume_state(
                        args.state_file, args.run_id, args.privacy, "running",
                        completed_titles=completed_titles, part_plan=part_plan,
                        pending_thumbnails=pending_thumbnails,
                    )

                preexisting_video_id = existing_video_ids.get(expected_title) if expected_title in completed_titles else None

                # This includes durable checkpoint entries and exact-title
                # progress recovered from the target playlist.
                if expected_title in completed_titles and part_counter in hf_archiver.completed_parts(book_title):
                    logging.info(f"⏭️ 【第 {part_counter} 部】(第 {s_c}~{e_c} 章) 已存在於 YouTube 播放清單，觸發【智能斷點續傳】秒跳過！")
                    publication.complete(part_counter, "archive_hf", recovered_from_hf=True, hf_repo=hf_repo)
                    for item in sliced_items:
                        try:
                            if os.path.exists(item["path"]):
                                os.remove(item["path"])
                        except Exception:
                            pass
                    sliced_paths = set(x["path"] for x in sliced_items)
                    chapter_pool = [x for x in chapter_pool if x["path"] not in sliced_paths]
                    part_counter += 1
                    continue

                out_name = f"{book_title}_Part_{part_counter:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.mp4"
                out_srt_name = f"{book_title}_Part_{part_counter:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.srt"
                out_path = os.path.join(temp_parts_dir, out_name)
                out_srt_path = os.path.join(upload_subtitles_dir, out_srt_name)

                publication.complete(
                    part_counter, "prepare_chapters",
                    chapter_count=len(sliced_items), expected_duration_seconds=round(sliced_dur, 3),
                )
                publication.mark(part_counter, "generate_subtitle", "running")
                srt_ok = generate_part_srt(sliced_items, out_srt_path)
                if not srt_ok or not os.path.exists(out_srt_path):
                    publication.fail(part_counter, "generate_subtitle", RuntimeError("Part SRT was not generated"))
                    logging.error("Required Part CC subtitle file was not generated; stopping before upload")
                    return 1
                srt_validation = validate_srt(out_srt_path, sliced_dur)
                publication.complete(part_counter, "generate_subtitle", **srt_validation)

                part_info = {
                    "part_num": part_counter,
                    "start_chap": s_c,
                    "end_chap": e_c,
                    "files": [x["path"] for x in sliced_items],
                    "duration": sliced_dur
                }

                logging.info(f"\n🚀 【第 {part_counter} 部】準備無縫合成: 第 {s_c}~{e_c} 章 ({len(sliced_items)} 章，總長 {sliced_dur/3600:.2f} 小時)")

                publication.mark(part_counter, "merge_video", "running")
                if merge_part_videos(part_info, out_path):
                    publication.complete(part_counter, "merge_video", output=out_path)
                    publication.mark(part_counter, "validate_video", "running")
                    video_validation = validate_video(out_path, sliced_dur)
                    publication.complete(part_counter, "validate_video", **video_validation)
                    publication.mark(part_counter, "generate_metadata_cover", "running")
                    p_meta = save_book_metadata(
                        book_title=book_title,
                        start_chap=s_c,
                        end_chap=e_c,
                        is_completed=True,
                        part_num=part_counter
                    )
                    cover_validation = validate_image(p_meta["cover_file"], expected_size=(1280, 720))
                    if cover_validation["bytes"] >= 2 * 1024 * 1024:
                        raise RuntimeError("YouTube cover exceeds the 2 MB limit")
                    publication.complete(
                        part_counter, "generate_metadata_cover",
                        title=p_meta["title"], cover=p_meta["cover_file"], cover_sha256=cover_validation["sha256"],
                    )
                    full_desc = (
                        f"{p_meta['description']}\n\n"
                        f"播放清單全集：https://www.youtube.com/playlist?list={playlist_id or ''}"
                    )

                    omitted = [int(value) for value in locked_part.get("source_missing_chapters", [])]
                    if omitted:
                        full_desc += (
                            "\n\n來源網站缺失章節（原頁面無文章，故未製作）："
                            + "、".join(str(value) for value in omitted)
                        )

                    publication.mark(part_counter, "archive_hf", "running")
                    hf_future = hf_executor.submit(
                        hf_archiver.archive_part,
                        book_title=book_title, part_num=part_counter,
                        start_chap=s_c, end_chap=e_c,
                        chapters=[int(item["chap_num"]) for item in sliced_items],
                        video_path=out_path, subtitle_path=out_srt_path,
                        master_cover_path=p_meta["master_cover_file"],
                        source_config_path=config_path, run_id=args.run_id,
                        task_id=args.task_id, source_missing_chapters=omitted,
                    )

                    # A YouTube-complete Part from an earlier attempt may still
                    # need its HF backup. Rebuild only the Part media, archive it,
                    # and reuse the existing Video ID without another upload.
                    if preexisting_video_id:
                        try:
                            hf_future.result()
                            archive_record = hf_archiver.finalize_part(
                                book_title=book_title, part_num=part_counter,
                                youtube_video_id=preexisting_video_id, playlist_id=playlist_id,
                                title=p_meta["title"], description=full_desc,
                                privacy=args.privacy, playlist_position=part_counter - 1,
                            )
                            publication.complete(part_counter, "archive_hf", hf_repo=hf_repo, path=archive_record["root"])
                            logging.info("[HF_ARCHIVE_MARKER] DONE | Part %s | Ch %s~%s | %s", part_counter, s_c, e_c, archive_record["root"])
                            for step in ("upload_video", "upload_thumbnail", "upload_caption", "add_playlist", "publish"):
                                publication.complete(part_counter, step, youtube_video_id=preexisting_video_id, recovered=True)
                            publication.complete(
                                part_counter, "final_validation",
                                **verify_published_part(youtube, preexisting_video_id, playlist_id, args.privacy),
                            )
                        except Exception as error:
                            publication.fail(part_counter, "archive_hf", error)
                            logging.error("HF archive recovery failed; retaining Part media: %s", error)
                            return 1
                        for item in sliced_items:
                            if os.path.exists(item["path"]):
                                os.remove(item["path"])
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        sliced_paths = {item["path"] for item in sliced_items}
                        chapter_pool = [item for item in chapter_pool if item["path"] not in sliced_paths]
                        part_counter += 1
                        continue

                    logging.info(f"[API_UPLOAD_MARKER] START | Part {part_counter} | Ch {s_c}~{e_c} | {out_name}")
                    sys.stdout.flush()

                    try:
                        publication.mark(part_counter, "upload_video", "running")
                        v_id = upload_video_file(
                            youtube,
                            video_path=out_path,
                            title=p_meta["title"],
                            description=full_desc,
                            # A video is never user-visible until every required
                            # post-upload action has committed successfully.
                            privacy_status="private",
                            cover_path=p_meta["cover_file"]
                        )
                    except ThumbnailUploadPaused as paused:
                        # videos.insert already succeeded.  Persist its id before
                        # stopping so resume repairs the cover without duplicating
                        # this multi-hour video.
                        pending_thumbnails[p_meta["title"]] = paused.video_id
                        pending_playlist[p_meta["title"]] = paused.video_id
                        publication.complete(part_counter, "upload_video", youtube_video_id=paused.video_id)
                        publication.fail(part_counter, "upload_thumbnail", paused, paused=True, youtube_video_id=paused.video_id)
                        save_resume_state(
                            args.state_file, args.run_id, args.privacy, "paused",
                            paused.reason, paused.retry_at, completed_titles, part_plan,
                            pending_thumbnails, pending_playlist=pending_playlist,
                            pending_captions=pending_captions,
                            pending_publish=pending_publish,
                        )
                        logging.error(
                            "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                            len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
                            args.run_id, paused.reason,
                        )
                        return EXIT_RETRY_LATER
                    except UploadPaused as paused:
                        publication.fail(part_counter, "upload_video", paused, paused=True)
                        save_resume_state(args.state_file, args.run_id, args.privacy, "paused",
                                          paused.reason, paused.retry_at, completed_titles, part_plan,
                                          pending_thumbnails)
                        logging.error(
                            "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                            len(completed_titles), len(part_plan), paused.retry_at.isoformat(), args.run_id, paused.reason,
                        )
                        logging.error("⏸️ 已安全暫停；下次可重試時間：%s", paused.retry_at.isoformat())
                        return EXIT_RETRY_LATER

                    if v_id:
                        publication.complete(part_counter, "upload_video", youtube_video_id=v_id)
                        publication.complete(part_counter, "upload_thumbnail", youtube_video_id=v_id)
                        total_uploaded += 1
                        pending_playlist[p_meta["title"]] = v_id
                        # Persist the video id before post-upload work so resume
                        # repairs this exact upload instead of uploading a duplicate.
                        save_resume_state(args.state_file, args.run_id, args.privacy, "running",
                                          completed_titles=completed_titles, part_plan=part_plan,
                                          pending_thumbnails=pending_thumbnails,
                                          pending_playlist=pending_playlist,
                                          pending_captions=pending_captions,
                                          pending_publish=pending_publish)
                        publication.mark(part_counter, "upload_caption", "running")
                        if not upload_caption_file(youtube, v_id, out_srt_path):
                            pending_captions[p_meta["title"]] = {
                                "video_id": v_id, "srt_path": out_srt_path,
                            }
                            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                            save_resume_state(
                                args.state_file, args.run_id, args.privacy, "paused",
                                "captionUploadFailed", retry_at, completed_titles,
                                part_plan, pending_thumbnails,
                                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                                pending_playlist=pending_playlist,
                                pending_captions=pending_captions,
                                pending_publish=pending_publish,
                            )
                            logging.error("CC subtitle upload is mandatory; private video retained for retry")
                            publication.fail(part_counter, "upload_caption", RuntimeError("caption upload failed"), paused=True, youtube_video_id=v_id)
                            return EXIT_RETRY_LATER
                        publication.complete(part_counter, "upload_caption", youtube_video_id=v_id)
                        publication.mark(part_counter, "add_playlist", "running")
                        if not add_video_to_playlist(youtube, playlist_id, v_id, position=part_counter - 1):
                            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                            save_resume_state(
                                args.state_file, args.run_id, args.privacy, "paused",
                                "playlistInsertFailed", retry_at, completed_titles,
                                part_plan, pending_thumbnails,
                                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                                pending_playlist=pending_playlist,
                            )
                            logging.error("Playlist insertion failed; stopping before the next Part")
                            publication.fail(part_counter, "add_playlist", RuntimeError("playlist insertion failed"), paused=True, youtube_video_id=v_id)
                            return EXIT_RETRY_LATER
                        publication.complete(part_counter, "add_playlist", youtube_video_id=v_id, position=part_counter - 1)
                        del pending_playlist[p_meta["title"]]
                        pending_publish[p_meta["title"]] = v_id
                        existing_titles.add(p_meta["title"])
                        publication.mark(part_counter, "publish", "running")
                        if not set_video_privacy(youtube, v_id, args.privacy):
                            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                            save_resume_state(
                                args.state_file, args.run_id, args.privacy, "paused",
                                "publishFailed", retry_at, completed_titles, part_plan,
                                pending_thumbnails,
                                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                                pending_playlist=pending_playlist,
                                pending_captions=pending_captions,
                                pending_publish=pending_publish,
                            )
                            publication.fail(part_counter, "publish", RuntimeError("final publish failed"), paused=True, youtube_video_id=v_id)
                            return EXIT_RETRY_LATER
                        publication.complete(part_counter, "publish", youtube_video_id=v_id, privacy=args.privacy)
                        del pending_publish[p_meta["title"]]
                        publication.mark(part_counter, "final_validation", "running")
                        evidence = verify_published_part(youtube, v_id, playlist_id, args.privacy)
                        publication.complete(part_counter, "final_validation", **evidence)
                        try:
                            hf_future.result()
                            archive_record = hf_archiver.finalize_part(
                                book_title=book_title, part_num=part_counter,
                                youtube_video_id=v_id, playlist_id=playlist_id,
                                title=p_meta["title"], description=full_desc,
                                privacy=args.privacy, playlist_position=part_counter - 1,
                            )
                            publication.complete(part_counter, "archive_hf", hf_repo=hf_repo, path=archive_record["root"])
                            logging.info("[HF_ARCHIVE_MARKER] DONE | Part %s | Ch %s~%s | %s", part_counter, s_c, e_c, archive_record["root"])
                        except Exception as error:
                            publication.fail(part_counter, "archive_hf", error)
                            logging.error("HF archive failed; YouTube is complete but Part media will be retained: %s", error)
                            return 1
                        completed_titles.add(p_meta["title"])
                        save_resume_state(
                            args.state_file, args.run_id, args.privacy, "running",
                            completed_titles=completed_titles, part_plan=part_plan,
                            pending_thumbnails=pending_thumbnails,
                            pending_playlist=pending_playlist,
                            pending_captions=pending_captions,
                            pending_publish=pending_publish,
                            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                        )

                        logging.info(f"[API_UPLOAD_MARKER] DONE | Part {part_counter} | Ch {s_c}~{e_c} | VideoID {v_id}, total {total_uploaded}")
                        logging.info(f"✅ 【第 {part_counter} 部】成功上傳並加入播放清單: {p_meta['title']}\n")
                        sys.stdout.flush()

                    if not v_id:
                        logging.error("❌ 未取得 Video ID，保留所有檔案並中止，避免斷點被錯誤推進。")
                        return 1

                    # 精準刪除已上傳完畢的單章與 Part 影片檔 (合併字幕保留於 Upload_Subtitles 目錄)
                    logging.info(f"🧹 釋放硬碟空間：清理【第 {part_counter} 部】已上傳完畢的 {len(sliced_items)} 個單章原始檔與 Part 大影片...")
                    for item in sliced_items:
                        try:
                            if os.path.exists(item["path"]):
                                os.remove(item["path"])
                        except Exception as e:
                            logging.warning(f"刪除暫存檔 {item['path']} 失敗: {e}")

                    if os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except Exception:
                            pass

                # 從緩衝池中移除已處理的章節
                sliced_paths = set(x["path"] for x in sliced_items)
                chapter_pool = [x for x in chapter_pool if x["path"] not in sliced_paths]

                part_counter += 1

                remaining_dur = sum(x["dur"] for x in chapter_pool)
                if remaining_dur <= max_seconds and not is_last_artifact:
                    logging.info(f"   剩餘 {len(chapter_pool)} 章 (約 {remaining_dur/3600:.2f} 小時)，繼續下載以固定分部邊界...")
                    break

            # 等待背景預載線程完成
            if prefetch_thread:
                prefetch_thread.join()

    elif args.input_dir and os.path.exists(args.input_dir):
        files_to_upload = sorted(glob.glob(os.path.join(args.input_dir, "**", "*.mp4"), recursive=True), key=lambda p: parse_chapter_info(os.path.basename(p)))
        if not files_to_upload:
            logging.error("❌ 未找到任何可供上傳的 MP4 影片檔案！")
            sys.exit(1)

        is_part_files = any("_Part_" in os.path.basename(p) for p in files_to_upload)
        parts_to_upload = []

        if not is_part_files:
            from part_builder import partition_chapters
            partitioned_parts = partition_chapters(files_to_upload, min_hours=10.0, max_hours=11.0)
            for p in partitioned_parts:
                part_num = p["part_num"]
                s_c = p["start_chap"]
                e_c = p["end_chap"]
                out_name = f"{book_title}_Part_{part_num:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.mp4"
                out_path = os.path.join(temp_parts_dir, out_name)
                if merge_part_videos(p, out_path):
                    p_meta = save_book_metadata(
                        book_title=book_title,
                        start_chap=s_c,
                        end_chap=e_c,
                        is_completed=True,
                        part_num=part_num
                    )
                    parts_to_upload.append({
                        "video_path": out_path,
                        "title": p_meta["title"],
                        "description": p_meta["description"],
                        "cover_path": p_meta["cover_file"],
                        "part_num": part_num,
                        "start_chap": s_c,
                        "end_chap": e_c
                    })
        else:
            for idx, vp in enumerate(files_to_upload, 1):
                c_start, c_end = parse_chapter_info(os.path.basename(vp))
                p_meta = save_book_metadata(
                    book_title=book_title,
                    start_chap=c_start,
                    end_chap=c_end,
                    is_completed=True,
                    part_num=idx
                )
                parts_to_upload.append({
                    "video_path": vp,
                    "title": p_meta["title"],
                    "description": p_meta["description"],
                    "cover_path": p_meta["cover_file"],
                    "part_num": idx,
                    "start_chap": c_start,
                    "end_chap": c_end
                })

        local_plan = []
        for item in parts_to_upload:
            local_plan.append({
                "part_num": item["part_num"], "start_chap": item["start_chap"],
                "end_chap": item["end_chap"],
                "chapters": list(range(item["start_chap"], item["end_chap"] + 1)),
                "duration": get_media_duration(item["video_path"]), "title": item["title"],
            })
        publication.lock_plan(local_plan, run_id=args.run_id, book_title=book_title)
        publication.mark_global("lock_plan", "completed", part_count=len(local_plan))
        part_plan = local_plan

        recovered_titles = recover_completed_titles_from_playlist(
            completed_titles,
            existing_titles,
            (item["title"] for item in parts_to_upload),
        )
        if recovered_titles:
            logging.warning(
                "Recovered %s/%s completed Parts from exact title matches in the target playlist; "
                "they will not be uploaded again.",
                len(recovered_titles), len(parts_to_upload),
            )

        for idx, item in enumerate(parts_to_upload, 1):
            v_path = item["video_path"]
            v_title = item["title"]
            v_desc = item["description"]
            v_cover = item["cover_path"]
            part_n = item["part_num"]

            if v_title in completed_titles:
                logging.info("⏭️ 已上傳，跳過：%s", v_title)
                continue

            # 尋找對應的 SRT 字幕檔 (優先搜尋 Upload_Subtitles 資料夾)
            v_srt_name = os.path.basename(v_path).replace(".mp4", ".srt")
            v_srt = os.path.join(upload_subtitles_dir, v_srt_name)
            if not os.path.exists(v_srt):
                v_srt = v_path.replace(".mp4", ".srt")
                if not os.path.exists(v_srt):
                    v_srt = None
            if not v_srt:
                logging.error("Required CC subtitle file is missing; stopping before upload: %s", v_title)
                return 1
            publication.complete(part_n, "prepare_chapters", chapter_count=item["end_chap"] - item["start_chap"] + 1)
            publication.complete(part_n, "generate_subtitle", **validate_srt(v_srt, get_media_duration(v_path)))
            publication.complete(part_n, "merge_video", output=v_path, reused_existing=True)
            publication.complete(part_n, "validate_video", **validate_video(v_path))
            cover_validation = validate_image(v_cover, expected_size=(1280, 720))
            publication.complete(part_n, "generate_metadata_cover", title=v_title, cover_sha256=cover_validation["sha256"])

            full_desc = f"{v_desc}\n\n播放清單全集：https://www.youtube.com/playlist?list={playlist_id or ''}"
            logging.info(f"[API_UPLOAD_MARKER] START | Part {part_n}/{len(parts_to_upload)} | Ch {item['start_chap']}~{item['end_chap']} | {os.path.basename(v_path)}")
            try:
                publication.mark(part_n, "upload_video", "running")
                v_id = upload_video_file(
                    youtube,
                    video_path=v_path,
                    title=v_title,
                    description=full_desc,
                    privacy_status="private",
                    cover_path=v_cover
                )
            except ThumbnailUploadPaused as paused:
                publication.complete(part_n, "upload_video", youtube_video_id=paused.video_id)
                publication.fail(part_n, "upload_thumbnail", paused, paused=True, youtube_video_id=paused.video_id)
                pending_thumbnails[v_title] = paused.video_id
                pending_playlist[v_title] = paused.video_id
                save_resume_state(
                    args.state_file, args.run_id, args.privacy, "paused",
                    paused.reason, paused.retry_at, completed_titles, part_plan,
                    pending_thumbnails, pending_playlist=pending_playlist,
                    pending_captions=pending_captions,
                    pending_publish=pending_publish,
                )
                logging.error(
                    "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                    len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
                    args.run_id, paused.reason,
                )
                return EXIT_RETRY_LATER
            except UploadPaused as paused:
                publication.fail(part_n, "upload_video", paused, paused=True)
                save_resume_state(args.state_file, args.run_id, args.privacy, "paused",
                                  paused.reason, paused.retry_at, completed_titles, part_plan,
                                  pending_thumbnails, playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None)
                logging.error(
                    "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                    len(completed_titles), len(part_plan), paused.retry_at.isoformat(), args.run_id, paused.reason,
                )
                return EXIT_RETRY_LATER
            if v_id:
                publication.complete(part_n, "upload_video", youtube_video_id=v_id)
                publication.complete(part_n, "upload_thumbnail", youtube_video_id=v_id)
                total_uploaded += 1
                pending_playlist[v_title] = v_id
                save_resume_state(args.state_file, args.run_id, args.privacy, "running",
                                  completed_titles=completed_titles, part_plan=part_plan,
                                  pending_thumbnails=pending_thumbnails,
                                  pending_playlist=pending_playlist,
                                  pending_captions=pending_captions,
                                  pending_publish=pending_publish,
                                  playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None)
                publication.mark(part_n, "upload_caption", "running")
                if not upload_caption_file(youtube, v_id, v_srt):
                    pending_captions[v_title] = {"video_id": v_id, "srt_path": v_srt}
                    retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                    save_resume_state(
                        args.state_file, args.run_id, args.privacy, "paused",
                        "captionUploadFailed", retry_at, completed_titles,
                        part_plan, pending_thumbnails,
                        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                        pending_playlist=pending_playlist,
                        pending_captions=pending_captions,
                        pending_publish=pending_publish,
                    )
                    publication.fail(part_n, "upload_caption", RuntimeError("caption upload failed"), paused=True, youtube_video_id=v_id)
                    return EXIT_RETRY_LATER
                publication.complete(part_n, "upload_caption", youtube_video_id=v_id)
                publication.mark(part_n, "add_playlist", "running")
                if not add_video_to_playlist(youtube, playlist_id, v_id, position=part_n - 1):
                    retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                    save_resume_state(
                        args.state_file, args.run_id, args.privacy, "paused",
                        "playlistInsertFailed", retry_at, completed_titles,
                        part_plan, pending_thumbnails,
                        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                        pending_playlist=pending_playlist,
                    )
                    logging.error("Playlist insertion failed; stopping before the next Part")
                    publication.fail(part_n, "add_playlist", RuntimeError("playlist insertion failed"), paused=True, youtube_video_id=v_id)
                    return EXIT_RETRY_LATER
                publication.complete(part_n, "add_playlist", youtube_video_id=v_id, position=part_n - 1)
                del pending_playlist[v_title]
                pending_publish[v_title] = v_id
                existing_titles.add(v_title)
                publication.mark(part_n, "publish", "running")
                if not set_video_privacy(youtube, v_id, args.privacy):
                    retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
                    save_resume_state(
                        args.state_file, args.run_id, args.privacy, "paused",
                        "publishFailed", retry_at, completed_titles, part_plan,
                        pending_thumbnails,
                        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                        pending_playlist=pending_playlist,
                        pending_captions=pending_captions,
                        pending_publish=pending_publish,
                    )
                    publication.fail(part_n, "publish", RuntimeError("final publish failed"), paused=True, youtube_video_id=v_id)
                    return EXIT_RETRY_LATER
                publication.complete(part_n, "publish", youtube_video_id=v_id, privacy=args.privacy)
                del pending_publish[v_title]
                completed_titles.add(v_title)
                publication.mark(part_n, "final_validation", "running")
                publication.complete(part_n, "final_validation", **verify_published_part(youtube, v_id, playlist_id, args.privacy))
                save_resume_state(
                    args.state_file, args.run_id, args.privacy, "running",
                    completed_titles=completed_titles, part_plan=part_plan,
                    pending_thumbnails=pending_thumbnails,
                    pending_playlist=pending_playlist,
                    pending_captions=pending_captions,
                    pending_publish=pending_publish,
                    playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                )
                logging.info(f"[API_UPLOAD_MARKER] DONE | Part {part_n}/{len(parts_to_upload)} | Ch {item['start_chap']}~{item['end_chap']} | VideoID {v_id}, total {total_uploaded}")

    if pending_thumbnails:
        raise RuntimeError(f"仍有 {len(pending_thumbnails)} 部影片等待補封面，禁止標記 complete")
    if pending_playlist:
        raise RuntimeError(f"仍有 {len(pending_playlist)} 部影片未加入播放清單，禁止標記 complete")
    if pending_captions:
        raise RuntimeError(f"仍有 {len(pending_captions)} 部影片缺少 YouTube CC 字幕，禁止標記 complete")
    if pending_publish:
        raise RuntimeError(f"仍有 {len(pending_publish)} 部影片尚未完成最終發布，禁止標記 complete")
    if args.run_id:
        expected_titles = {str(part.get("title") or "").strip() for part in part_plan}
        expected_titles.discard("")
        finished_titles = completed_titles
        missing_titles = sorted(expected_titles - finished_titles)
        if missing_titles:
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "running",
                reason="incompleteParts", completed_titles=completed_titles,
                part_plan=part_plan, pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None
            )
            logging.error(
                "[API_UPLOAD_STATUS] INCOMPLETE | finished=%s | total=%s | missing=%s",
                len(expected_titles) - len(missing_titles), len(expected_titles), missing_titles[:3],
            )
            return EXIT_RETRY_LATER

        # Check every planned Part against YouTube again at the very end. A
        # restored checkpoint or an exact-title recovery is not success by
        # itself; the public video, CC track, thumbnail and playlist entry must
        # all still be readable.
        final_playlist_index = get_playlist_video_index(youtube, playlist_id)
        for planned in part_plan:
            title = str(planned.get("title") or "").strip()
            video_id = final_playlist_index.get(title)
            if not video_id:
                raise RuntimeError(f"final YouTube validation cannot find planned Part in playlist: {title}")
            evidence = verify_published_part(youtube, video_id, playlist_id, args.privacy)
            part_num = int(planned["part_num"])
            record = publication.data.get("parts", {}).get(str(part_num), {})
            steps = record.get("steps") or {}
            for step in PART_STEPS:
                if step == "archive_hf":
                    continue
                if (steps.get(step) or {}).get("status") != "completed":
                    publication.complete(
                        part_num,
                        step,
                        recovered_from_youtube=True,
                        youtube_video_id=video_id,
                    )
            publication.complete(part_num, "final_validation", **evidence)

        hf_evidence = hf_archiver.verify_book(book_title, len(part_plan))
        for planned in part_plan:
            part_num = int(planned["part_num"])
            record = publication.data.get("parts", {}).get(str(part_num), {})
            if ((record.get("steps") or {}).get("archive_hf") or {}).get("status") != "completed":
                raise RuntimeError(f"Part {part_num} is missing mandatory Hugging Face archive evidence")
        logging.info(
            "[HF_ARCHIVE_STATUS] COMPLETE | archived=%s | total=%s | repo=%s | folder=%s",
            hf_evidence["parts"], len(part_plan), hf_evidence["repo_id"], hf_evidence["book_root"],
        )

    logging.info("="*60)
    logging.info(f"🎉 全部影片極速上傳完畢！共上傳 {total_uploaded} 部分部影片至 YouTube 播放清單！")
    if playlist_id:
        logging.info(f"👉 播放清單網址: https://www.youtube.com/playlist?list={playlist_id}")
    logging.info("="*60)
    save_resume_state(args.state_file, args.run_id, args.privacy, "complete",
                      completed_titles=completed_titles, part_plan=part_plan,
                      pending_thumbnails=pending_thumbnails,
                      pending_playlist=pending_playlist,
                      pending_captions=pending_captions,
                      pending_publish=pending_publish,
                      playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None)
    publication.mark_global("final_book_validation", "completed", completed_parts=len(completed_titles))
    hf_executor.shutdown(wait=True)
    logging.info(
        "[API_UPLOAD_STATUS] COMPLETE | uploaded=%s | total=%s | source_run=%s",
        len(completed_titles), len(part_plan) or len(completed_titles), args.run_id or "local",
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
