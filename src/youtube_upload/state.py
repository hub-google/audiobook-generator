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


try:
    from ..publication_checkpoint import plan_fingerprint
except ImportError:
    from publication_checkpoint import plan_fingerprint


def validate_state_identity(saved_state, book_profile_id="", part_plan=None, plan_fingerprint_str="", task_id=""):
    """Verify whether a restored state dictionary matches expected fingerprint identity."""
    if not isinstance(saved_state, dict):
        return True, ""
    if book_profile_id and saved_state.get("book_profile_id"):
        if str(saved_state.get("book_profile_id")).strip() != str(book_profile_id).strip():
            return False, f"checkpoint book_profile_id {saved_state.get('book_profile_id')!r} != expected {book_profile_id!r}"
    expected_fp = plan_fingerprint_str or (plan_fingerprint(part_plan) if part_plan else "")
    if expected_fp:
        state_fp = saved_state.get("plan_fingerprint") or (plan_fingerprint(saved_state.get("part_plan", [])) if saved_state.get("part_plan") else "")
        if state_fp and state_fp != expected_fp:
            return False, f"checkpoint plan_fingerprint {state_fp!r} != expected {expected_fp!r}"
    return True, ""


def save_resume_state(path, run_id, privacy, status, reason="", retry_at=None,
                      completed_titles=None, part_plan=None, pending_thumbnails=None,
                      playlist_url=None, pending_playlist=None,
                      pending_captions=None, pending_publish=None,
                      final_playlist_validation=None,
                      task_id=None, book_title=None, book_profile_id=None,
                      source_run_id=None, execution_run_id=None,
                      plan_fingerprint_str=None):
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

    resolved_source_run_id = (
        source_run_id
        or (previous or {}).get("source_run_id")
        or (previous or {}).get("run_id")
        or run_id
        or ""
    )
    resolved_execution_run_id = (
        execution_run_id
        or os.environ.get("GITHUB_RUN_ID", "")
        or run_id
        or (previous or {}).get("execution_run_id")
        or ""
    )
    resolved_task_id = (
        task_id
        or os.environ.get("QUEUE_TASK_ID", "").strip()
        or (previous or {}).get("task_id")
        or ""
    )
    resolved_book_profile_id = (
        book_profile_id
        or (previous or {}).get("book_profile_id")
        or ""
    )
    resolved_book_title = (
        book_title
        or (previous or {}).get("book_title")
        or ""
    )
    parts_list = list(part_plan or []) if part_plan is not None else list((previous or {}).get("part_plan") or [])
    resolved_fingerprint = plan_fingerprint_str or (plan_fingerprint(parts_list) if parts_list else (previous or {}).get("plan_fingerprint") or "")

    data = {
        "version": 4,
        "run_id": str(resolved_source_run_id or resolved_execution_run_id or run_id or ""),
        "source_run_id": str(resolved_source_run_id),
        "execution_run_id": str(resolved_execution_run_id),
        "task_id": str(resolved_task_id),
        "book_profile_id": str(resolved_book_profile_id),
        "book_title": str(resolved_book_title),
        "plan_fingerprint": str(resolved_fingerprint),
        "privacy": privacy,
        "status": status,
        "reason": reason,
        "retry_at": retry_at.isoformat() if retry_at else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "completed_titles": sorted(completed_titles or []),
        "part_plan": parts_list,
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
