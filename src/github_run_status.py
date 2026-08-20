"""Lossless GitHub Actions Run observations for the desktop GUI."""

from __future__ import annotations

from datetime import datetime, timezone


RUN_STATUS_LABELS = {
    "requested": "requested",
    "queued": "queued",
    "pending": "pending",
    "waiting": "waiting",
    "in_progress": "in_progress",
}

RUN_CONCLUSION_LABELS = {
    "success": "success",
    "cancelled": "cancelled",
    "failure": "failure",
    "timed_out": "timed_out",
    "action_required": "action_required",
    "stale": "stale",
    "neutral": "neutral",
    "skipped": "skipped",
    "startup_failure": "startup_failure",
}

ERROR_LABELS = {
    "unauthorized": "unauthorized",
    "forbidden": "forbidden",
    "rate_limited": "rate_limited",
    "github_error": "github_error",
    "network_error": "network_error",
    "invalid_response": "invalid_response",
    "stale": "stale",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def successful_observation(run_data, checked_at=None):
    return {
        "kind": "ok",
        "checked_at": checked_at or utc_now(),
        "http_status": 200,
        "raw_status": run_data.get("status"),
        "raw_conclusion": run_data.get("conclusion"),
        "github_updated_at": run_data.get("updated_at"),
        "run_attempt": run_data.get("run_attempt"),
        "not_found_count": 0,
    }


def missing_observation(previous=None, checked_at=None, confirmed=False):
    count = int((previous or {}).get("not_found_count") or 0) + 1
    return {
        "kind": "not_found",
        "checked_at": checked_at or utc_now(),
        "http_status": 404,
        "raw_status": None,
        "raw_conclusion": None,
        "not_found_count": count,
        "confirmed_missing": bool(confirmed and count >= 2),
    }


def error_observation(code, http_status=None, detail=None, checked_at=None):
    return {
        "kind": "error",
        "checked_at": checked_at or utc_now(),
        "http_status": http_status,
        "error_code": code,
        "error_detail": str(detail or ""),
        "raw_status": None,
        "raw_conclusion": None,
        "not_found_count": 0,
    }


def observation_is_fresh(observation, now=None, ttl_seconds=30):
    if not observation or not observation.get("checked_at"):
        return False
    try:
        checked = datetime.fromisoformat(str(observation["checked_at"]).replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        return (current - checked).total_seconds() <= ttl_seconds
    except (TypeError, ValueError):
        return False


def observation_text(observation, now=None, ttl_seconds=30):
    if not observation:
        return "unverified"
    if not observation_is_fresh(observation, now=now, ttl_seconds=ttl_seconds):
        return "stale"
    kind = observation.get("kind")
    if kind == "error":
        code = observation.get("error_code") or "invalid_response"
        return f"error: {ERROR_LABELS.get(code, code)}"
    if kind == "not_found":
        return "not_found" if observation.get("confirmed_missing") else "checking"
    status = observation.get("raw_status")
    conclusion = observation.get("raw_conclusion")
    if status == "completed":
        if conclusion is None:
            return "completed"
        return RUN_CONCLUSION_LABELS.get(conclusion, conclusion or "completed")
    if status in RUN_STATUS_LABELS:
        return RUN_STATUS_LABELS[status]
    return status or "unknown"
