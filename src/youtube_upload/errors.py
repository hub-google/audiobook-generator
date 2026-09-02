"""YouTube quota classification and retry policy."""

import socket
import ssl
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from googleapiclient.http import HttpError


class UploadPaused(RuntimeError):
    def __init__(self, reason, retry_at, original_error=None):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at
        self.original_error = original_error


class ThumbnailUploadPaused(UploadPaused):
    def __init__(self, video_id, retry_at, original_error=None, reason="thumbnailRateLimit"):
        super().__init__(reason, retry_at, original_error)
        self.video_id = video_id


def classify_daily_limit(error):
    text = str(error)
    now = datetime.now(timezone.utc)
    if "uploadLimitExceeded" in text:
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
    if isinstance(error, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError)):
        return True
    if isinstance(error, HttpError):
        return getattr(getattr(error, "resp", None), "status", None) in {429, 500, 502, 503, 504}
    return isinstance(error, OSError)


def is_transient_youtube_api_error(error):
    if isinstance(error, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError, OSError)):
        return True
    if not isinstance(error, HttpError):
        return False
    status = getattr(getattr(error, "resp", None), "status", None)
    if status in {429, 500, 502, 503, 504}:
        return True
    content = getattr(error, "content", b"")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return status == 409 and "SERVICE_UNAVAILABLE" in f"{error} {content}".upper()

