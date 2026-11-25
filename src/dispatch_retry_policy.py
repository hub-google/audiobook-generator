"""Pure retry classification for authoritative GitHub queue reconciliation."""
from datetime import datetime, timedelta, timezone

TRANSIENT_REASONS = frozenset({
    "quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded",
    "backendError", "otherError", "thumbnailRateLimit",
    "captionUploadFailed", "playlistInsertFailed", "publishFailed",
    "retryable YouTube API failure",
})


def retry_state(reason, suggested_at=None, existing_at=None, *, now=None):
    """Choose a durable next state without mutating a task or reading GitHub."""
    if reason not in TRANSIENT_REASONS:
        return {
            "status": "needs_attention",
            "reason": reason or "permanentFailure",
            "retry_at": None,
        }
    current = now if now is not None else datetime.now(timezone.utc)
    time_point = existing_at or suggested_at or (current + timedelta(hours=2))
    return {
        "status": "waiting_retry",
        "reason": reason,
        "retry_at": time_point.isoformat(),
    }
