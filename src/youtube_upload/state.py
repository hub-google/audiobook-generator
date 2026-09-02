"""Durable resume-state handling for YouTube publication."""

import json
import logging
import os
from datetime import datetime, timezone

MAX_YOUTUBE_ACCOUNT_SLOTS = 10


def configured_youtube_account_slots():
    """Return complete environment-backed credential slots without authenticating."""
    slots = set()
    for slot in range(1, MAX_YOUTUBE_ACCOUNT_SLOTS + 1):
        if all(os.environ.get(f"{name}_{slot}", "").strip() for name in (
            "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        )):
            slots.add(slot)
    return slots


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_resume_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_resume_state(path, run_id, privacy, status, reason="", retry_at=None,
                      completed_titles=None, part_plan=None, pending_thumbnails=None,
                      playlist_url=None, pending_playlist=None,
                      pending_captions=None, pending_publish=None,
                      final_playlist_validation=None):
    previous = load_resume_state(path)
    if (
        os.environ.get("MANUAL_YOUTUBE_RETRY", "").lower() == "true"
        and status == "paused"
        and reason == "quotaExceeded"
        and retry_at is not None
    ):
        previous_retry_text = (previous or {}).get("retry_at")
        if (previous or {}).get("reason") == "quotaExceeded" and previous_retry_text:
            try:
                previous_retry_at = datetime.fromisoformat(
                    previous_retry_text.replace("Z", "+00:00")
                )
                if datetime.now(timezone.utc) < previous_retry_at:
                    logging.info(
                        "🕒 手動提前測試仍為 quotaExceeded；保留原安全重試時間 %s。",
                        previous_retry_at.isoformat(),
                    )
                    retry_at = previous_retry_at
            except ValueError:
                logging.warning("忽略無法解析的舊 retry_at：%s", previous_retry_text)

    data = {
        "version": 4,
        "run_id": str(run_id) if run_id else "",
        "task_id": os.environ.get("QUEUE_TASK_ID", "").strip(),
        "privacy": privacy,
        "status": status,
        "reason": reason,
        "retry_at": retry_at.isoformat() if retry_at else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "completed_titles": sorted(completed_titles or []),
        "part_plan": list(part_plan or []),
        "pending_thumbnails": dict(pending_thumbnails or {}),
        "pending_playlist": dict(pending_playlist or {}),
        "pending_captions": dict(pending_captions or {}),
        "pending_publish": dict(pending_publish or {}),
        "playlist_url": playlist_url,
        "final_playlist_validation": dict(final_playlist_validation or {}),
        "credential_pool_size": len(configured_youtube_account_slots()),
    }
    atomic_write_json(path, data)
    logging.info("💾 上傳斷點已儲存：%s (%s)", path, status)


def recover_completed_titles_from_playlist(completed_titles, existing_titles, planned_titles):
    """Rebuild progress from exact planned-title matches in the target playlist."""
    recovered = set(planned_titles) & set(existing_titles)
    completed_titles.update(recovered)
    return recovered
