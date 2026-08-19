"""Lossless GitHub Actions Run observations for the desktop GUI."""

from __future__ import annotations

from datetime import datetime, timezone


RUN_STATUS_LABELS = {
    "requested": "已建立，等待排程",
    "queued": "等待 Runner",
    "pending": "等待執行限制解除",
    "waiting": "等待人工核准",
    "in_progress": "執行中",
}

RUN_CONCLUSION_LABELS = {
    "success": "Run 已成功",
    "cancelled": "執行中斷｜Run 已取消",
    "failure": "執行失敗",
    "timed_out": "執行逾時",
    "action_required": "需要人工處理",
    "stale": "Run 已過期",
    "neutral": "Run 中性結束",
    "skipped": "Run 已略過",
    "startup_failure": "Run 啟動失敗",
}

ERROR_LABELS = {
    "unauthorized": "GitHub Token 無效",
    "forbidden": "GitHub 權限不足",
    "rate_limited": "GitHub API 限流",
    "github_error": "GitHub 服務異常",
    "network_error": "無法連線 GitHub",
    "invalid_response": "GitHub 回應無法解析",
    "stale": "查證資料已過期",
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
        return "尚未查證 GitHub"
    if not observation_is_fresh(observation, now=now, ttl_seconds=ttl_seconds):
        return "無法確認｜查證資料已過期"
    kind = observation.get("kind")
    if kind == "error":
        code = observation.get("error_code") or "invalid_response"
        return f"無法確認｜{ERROR_LABELS.get(code, code)}"
    if kind == "not_found":
        return "Run 已不存在" if observation.get("confirmed_missing") else "API 查無 Run｜複查中"
    status = observation.get("raw_status")
    conclusion = observation.get("raw_conclusion")
    if status == "completed":
        if conclusion is None:
            return "Run 已結束｜等待結果同步"
        return RUN_CONCLUSION_LABELS.get(conclusion, f"未知結果｜{conclusion}")
    if status in RUN_STATUS_LABELS:
        return RUN_STATUS_LABELS[status]
    return f"未知狀態｜{status or '空值'}"
