"""Strict, machine-checkable acceptance criteria for a published audiobook."""

from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlparse

try:
    from .publication_checkpoint import GLOBAL_STEPS, PART_STEPS
except ImportError:
    from publication_checkpoint import GLOBAL_STEPS, PART_STEPS


PENDING_FIELDS = (
    "pending_thumbnails",
    "pending_playlist",
    "pending_captions",
    "pending_publish",
)


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"required success evidence is unavailable: {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"required success evidence is not a JSON object: {path}")
    return value


def _valid_playlist_url(value):
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc in {"youtube.com", "www.youtube.com"}
        and parsed.path == "/playlist"
        and bool(parse_qs(parsed.query).get("list", [""])[0])
    )


def validate_upload_success(state_file, expected_run_id=None):
    """Reject every state except a complete, externally published Part set."""
    state = _load_json(state_file)
    errors = []

    if state.get("status") != "complete":
        errors.append(f"status is {state.get('status')!r}, not 'complete'")
    if expected_run_id is not None and str(state.get("run_id") or "") != str(expected_run_id):
        errors.append(
            f"checkpoint run_id {state.get('run_id')!r} does not match source run {expected_run_id!r}"
        )

    plan = state.get("part_plan")
    if not isinstance(plan, list) or not plan:
        errors.append("Part plan is empty")
        plan = []
    planned_titles = [str(part.get("title") or "").strip() for part in plan if isinstance(part, dict)]
    if len(planned_titles) != len(plan) or any(not title for title in planned_titles):
        errors.append("every planned Part must have a non-empty title")
    if len(set(planned_titles)) != len(planned_titles):
        errors.append("Part titles are not unique")

    completed = [str(title).strip() for title in state.get("completed_titles") or []]
    if set(completed) != set(planned_titles) or len(completed) != len(planned_titles):
        errors.append(
            f"completed Parts do not exactly match the locked plan ({len(completed)}/{len(planned_titles)})"
        )

    for field in PENDING_FIELDS:
        value = state.get(field) or {}
        if not isinstance(value, dict) or value:
            errors.append(f"{field} is not empty")

    if not _valid_playlist_url(state.get("playlist_url")):
        errors.append("a valid YouTube playlist URL is missing")

    viewer_gate = state.get("final_playlist_validation") or {}
    if viewer_gate.get("status") != "passed":
        errors.append("the final user-facing playlist gate did not pass")
    if viewer_gate.get("item_count") != len(plan):
        errors.append("the user-facing playlist item count does not match the Part plan")
    if viewer_gate.get("ordered_parts") != list(range(1, len(plan) + 1)):
        errors.append("the user-facing playlist is not ordered from the first Part to the last Part")
    if viewer_gate.get("unique_video_ids") != len(plan):
        errors.append("the user-facing playlist contains missing or duplicate videos")
    cover_sha = str(viewer_gate.get("canonical_cover_sha256") or "")
    if len(cover_sha) != 64 or any(char not in "0123456789abcdef" for char in cover_sha.lower()):
        errors.append("a single verified canonical-cover SHA-256 is missing")

    execution_path = os.path.join(os.path.dirname(os.path.abspath(state_file)), "part_execution.json")
    execution = _load_json(execution_path)
    if execution.get("plan_status") != "locked":
        errors.append("publication Part plan is not locked")
    if expected_run_id is not None and str(execution.get("source_run_id") or "") != str(expected_run_id):
        errors.append("publication ledger belongs to a different source run")

    global_steps = execution.get("global_steps") or {}
    for step in GLOBAL_STEPS:
        if (global_steps.get(step) or {}).get("status") != "completed":
            errors.append(f"global publication step {step} is not completed")

    part_records = execution.get("parts") or {}
    for part in plan:
        if not isinstance(part, dict) or "part_num" not in part:
            continue
        record = part_records.get(str(int(part["part_num"]))) or {}
        if record.get("overall_status") != "completed":
            errors.append(f"Part {part['part_num']} publication ledger is not completed")
        steps = record.get("steps") or {}
        for step in PART_STEPS:
            if (steps.get(step) or {}).get("status") != "completed":
                errors.append(f"Part {part['part_num']} step {step} is not completed")

    if errors:
        raise RuntimeError("Strict success gate failed: " + "; ".join(errors))
    return {
        "run_id": str(state.get("run_id") or ""),
        "parts": len(plan),
        "playlist_url": state["playlist_url"],
        "canonical_cover_sha256": cover_sha,
    }
