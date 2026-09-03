"""Strict, machine-checkable acceptance criteria for a published audiobook."""

from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlparse

try:
    from .publication_checkpoint import GLOBAL_STEPS, PART_STEPS, plan_fingerprint
except ImportError:
    from publication_checkpoint import GLOBAL_STEPS, PART_STEPS, plan_fingerprint


PENDING_FIELDS = (
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


def validate_upload_success(
    state_file,
    expected_run_id=None,
    execution_run_id=None,
    expected_task_id=None,
    expected_book_title=None,
    expected_book_profile_id=None,
    expected_plan_fingerprint=None,
    expected_part_plan=None,
):
    """Reject every state except a complete, externally published Part set."""
    state = _load_json(state_file)
    execution_path = os.path.join(os.path.dirname(os.path.abspath(state_file)), "part_execution.json")
    execution = _load_json(execution_path)
    errors = []

    if state.get("status") != "complete":
        errors.append(f"status is {state.get('status')!r}, not 'complete'")

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

    if execution.get("plan_status") != "locked":
        errors.append("publication Part plan is not locked")

    # 1. Internal identity consistency between state.json and part_execution.json
    state_fp = state.get("plan_fingerprint") or (plan_fingerprint(plan) if plan else "")
    ledger_plan = execution.get("plan") or []
    ledger_fp = execution.get("plan_fingerprint") or (plan_fingerprint(ledger_plan) if ledger_plan else "")
    if state_fp and ledger_fp and state_fp != ledger_fp:
        errors.append("checkpoint part_plan fingerprint does not match publication ledger plan")
    if state.get("book_profile_id") and execution.get("book_profile_id") and str(state["book_profile_id"]).strip() != str(execution["book_profile_id"]).strip():
        errors.append(f"checkpoint book_profile_id {state['book_profile_id']!r} does not match publication ledger book_profile_id {execution['book_profile_id']!r}")

    # 2. External fingerprint / identity verification
    task_identity_verified = False
    resolved_expected_profile = expected_book_profile_id or os.environ.get("BOOK_PROFILE_ID", "").strip() or None
    if resolved_expected_profile:
        chk_profile = execution.get("book_profile_id") or state.get("book_profile_id")
        if chk_profile and str(chk_profile).strip() != str(resolved_expected_profile).strip():
            errors.append(f"book_profile_id {chk_profile!r} does not match expected profile {resolved_expected_profile!r}")
        elif chk_profile:
            task_identity_verified = True

    resolved_expected_fp = expected_plan_fingerprint or (plan_fingerprint(expected_part_plan) if expected_part_plan else None)
    if resolved_expected_fp:
        chk_fp = execution.get("plan_fingerprint") or ledger_fp or state_fp
        if chk_fp and chk_fp != str(resolved_expected_fp).strip():
            errors.append("part plan fingerprint does not match expected plan fingerprint")
        elif chk_fp:
            task_identity_verified = True
    elif state_fp and ledger_fp and state_fp == ledger_fp:
        # Checkpoint and ledger share the exact same locked plan fingerprint
        task_identity_verified = True

    # 3. Run ID audit and verification
    chk_source_run_id = str(execution.get("source_run_id") or state.get("source_run_id") or "")
    chk_exec_run_id = str(execution_run_id or execution.get("execution_run_id") or state.get("execution_run_id") or "")
    chk_state_run_id = str(state.get("run_id") or "")

    if expected_run_id is not None:
        exp_run_str = str(expected_run_id)
        run_matches = exp_run_str in {chk_source_run_id, chk_exec_run_id, chk_state_run_id}
        if not run_matches and not task_identity_verified:
            errors.append(
                f"checkpoint run_id {chk_state_run_id!r} does not match source run {exp_run_str!r}"
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
        "run_id": str(state.get("run_id") or chk_source_run_id),
        "source_run_id": chk_source_run_id or str(state.get("run_id") or ""),
        "execution_run_id": chk_exec_run_id,
        "task_id": str(execution.get("task_id") or state.get("task_id") or ""),
        "parts": len(plan),
        "playlist_url": state["playlist_url"],
    }
