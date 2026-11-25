"""Authoritative GitHub queue reconciliation: status changes happen only here."""

def reconcile(dispatcher, queue):
    from .dispatch_retry_policy import retry_state
    from .queue_dispatcher import (
        BLOCKING_STATES, MAX_NATIVE_PREFLIGHT_RERUNS,
        parse_time, preflight_status, task_id_from_run_name, settle_interrupted_task,
        update_task, datetime, timedelta, timezone, requests,
    )
    runs = dispatcher.runs()
    triggered_audiobook = dispatcher.triggered_run("Audiobook Automation Pipeline (Parallel)")
    triggered_cover = dispatcher.triggered_run("Cover preflight")
    cover_runs = dispatcher.cover_runs() if any(
        task.get("workflow_phase") == "preflight" for task in queue.get("queue", [])
    ) else []
    by_task = {}
    for run in runs:
        task_id = task_id_from_run_name(run.get("display_title") or run.get("name"))
        if task_id:
            by_task.setdefault(task_id, []).append(run)
    covers_by_task = {}
    for run in cover_runs:
        task_id = task_id_from_run_name(run.get("display_title") or run.get("name"))
        if task_id:
            covers_by_task.setdefault(task_id, []).append(run)
    changed = False
    for task in list(queue["queue"]):
        if task.get("workflow_phase") == "preflight":
            if task.get("status") in {"queued", "paused", "stopped", "interrupted"}:
                continue
            task_id = task.get("task_id")
            stages = task.setdefault("stages", {})
            scrape = stages.setdefault("scrape", {})
            cover = stages.setdefault("cover", {})
            def stage_run(state, candidates, trigger=None):
                if state.get("run_id"):
                    bound = next((r for r in candidates if int(r["id"]) == int(state["run_id"])), None)
                    if trigger and int(state["run_id"]) == int(trigger["id"]):
                        return dispatcher.apply_completion_event(bound, trigger, state.get("run_attempt"))
                    return bound or dispatcher.run_by_id(state["run_id"])
                since = parse_time(state.get("dispatched_at") or task.get("dispatched_at"))
                if since:
                    since = since.replace(microsecond=0)
                return next((r for r in candidates if not since or
                             (parse_time(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since), None)
            scrape_run = stage_run(scrape, by_task.get(task_id) or [], triggered_audiobook)
            cover_run = stage_run(cover, covers_by_task.get(task_id) or [], triggered_cover)
            for name, state, run in (("scrape", scrape, scrape_run), ("cover", cover, cover_run)):
                if not run:
                    stamp = parse_time(state.get("dispatched_at") or task.get("dispatched_at"))
                    if state.get("status") == "dispatching" and stamp and datetime.now(timezone.utc) - stamp >= timedelta(minutes=30):
                        state.update(status="failed", reason="dispatch_unconfirmed")
                        changed = True
                    continue
                run_id = int(run["id"])
                raw_status = run.get("status")
                conclusion = run.get("conclusion")
                next_status = "running" if raw_status != "completed" else (
                    "completed" if conclusion == "success" else "failed"
                )
                attempt = int(run.get("run_attempt") or 1)
                if state.get("run_id") != run_id or state.get("status") != next_status or state.get("run_attempt") != attempt:
                    state.update({"run_id": run_id, "status": next_status, "run_attempt": attempt})
                    if next_status == "failed":
                        state["reason"] = conclusion or "failure"
                    elif next_status == "completed":
                        state.update({"reason": None, "completed_at": run.get("updated_at")})
                    task[f"{name}_run_id"] = run_id
                    changed = True
            statuses = {scrape.get("status"), cover.get("status")}
            if task.get("status") == "canceling":
                # A user may cancel during the short window before GitHub
                # indexes either workflow_dispatch Run. Keep the durable
                # task in canceling until both stage dispatches are
                # observed or explicitly fail, then stop it.
                for run in (scrape_run, cover_run):
                    if run and run.get("status") != "completed":
                        response = requests.post(
                            f"{dispatcher.api}/actions/runs/{int(run['id'])}/force-cancel",
                            headers=dispatcher.headers, timeout=30,
                        )
                        if response.status_code not in (200, 202, 409):
                            raise RuntimeError(
                                f"GitHub API POST /actions/runs/{run['id']}/force-cancel failed "
                                f"({response.status_code}): {response.text}"
                            )
                cancel_pending = any(
                    state.get("status") in {"running", "dispatching"}
                    for state in (scrape, cover)
                )
                new_status, reason = ("canceling" if cancel_pending else "stopped"), "user_cancelled"
            else:
                new_status, reason = preflight_status(stages)
            if task.get("status") != new_status or task.get("reason") != reason:
                task.update({"status": new_status, "reason": reason})
                changed = True
            continue
        # Recover the GUI/dispatcher race where GUI records the cancelled
        # Run first.  The durable edit intent must still win.
        if task.get("status") == "interrupted" and task.get("requeue_after_edit"):
            queue = settle_interrupted_task(queue, task["task_id"])
            changed = True
            continue
        candidates = by_task.get(task.get("task_id"), [])
        if task.get("scrape_run_id"):
            candidates = [r for r in candidates if int(r["id"]) != int(task["scrape_run_id"])
                          and "【TXT抓取】" not in str(r.get("display_title") or r.get("name") or "")]
        retry_requested_at = parse_time(task.get("retry_requested_at"))
        if retry_requested_at:
            candidates = [run for run in candidates if (parse_time(run.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= retry_requested_at]
        run = candidates[0] if candidates else None
        if (triggered_audiobook
                and int(task.get("run_id") or 0) == int(triggered_audiobook["id"])):
            run = dispatcher.apply_completion_event(run if run and int(run['id']) == int(triggered_audiobook['id']) else None, triggered_audiobook, task.get('run_attempt'))
        if not run:
            bound_run_id = task.get("run_id")
            if bound_run_id and task.get("status") in BLOCKING_STATES:
                run = dispatcher.run_by_id(bound_run_id)
                if run is None:
                    queue = settle_interrupted_task(
                        queue, task["task_id"], reason="run_not_found",
                        conclusion="missing", ended_at=datetime.now(timezone.utc).isoformat(),
                    )
                    changed = True
                    continue
            if not run:
                stamp = parse_time(task.get("dispatched_at"))
                if task.get("status") == "dispatching" and stamp and datetime.now(timezone.utc) - stamp >= timedelta(minutes=30):
                    task.update(status="needs_attention", reason="dispatch_unconfirmed")
                    changed = True
                continue
        run_id = int(run["id"])
        status = run.get("status")
        conclusion = run.get("conclusion")
        if task.get("run_id") != run_id:
            task["run_id"] = run_id
            changed = True
        if task.get("requeue_after_edit") and status != "completed":
            cancel_response = requests.post(
                f"{dispatcher.api}/actions/runs/{run_id}/force-cancel", headers=dispatcher.headers, timeout=30,
            )
            if cancel_response.status_code not in (200, 202, 409):
                raise RuntimeError(
                    f"GitHub API POST /actions/runs/{run_id}/force-cancel failed "
                    f"({cancel_response.status_code}): {cancel_response.text}"
                )
            if task.get("status") != "canceling":
                task["status"] = "canceling"
                changed = True
            continue
        progress = None if status == "completed" and conclusion == "cancelled" else dispatcher.progress_markers(run_id)
        if progress:
            for key, value in progress.items():
                if task.get(key) != value:
                    task[key] = value
                    changed = True
        if status != "completed":
            if task.get("status") != "running":
                task["status"] = "running"
                task["run_conclusion"] = None
                task["reason"] = None
                changed = True
        elif status == "completed" and conclusion == "success" and task.get("status") != "completed":
            queue = update_task(
                queue, task["task_id"], status="completed", reason=None,
                retry_at=None, completed_at=run.get("updated_at"),
                run_completed_at=run.get("updated_at"), run_conclusion="success",
            )
            changed = True
        elif status == "completed" and conclusion == "failure" and task.get("status") not in {"stopped", "paused"}:
            if task.get("status") == "waiting_retry" and task.get("retry_at"):
                continue
            existing_retry_at = parse_time(task.get("retry_at"))
            reason, retry_at = dispatcher.retry_marker(run_id)
            task.update(retry_state(reason, suggested_at=retry_at, existing_at=existing_retry_at))
            changed = True
            if task["status"] == "needs_attention":
                continue
        elif status == "completed" and conclusion == "cancelled" and task.get("status") != "paused":
            queue = settle_interrupted_task(
                queue, task["task_id"], reason="run_cancelled",
                conclusion="cancelled", ended_at=run.get("updated_at"),
            )
            changed = True
    for task in list(queue.get("completed", [])):
        hf = task.get("hf_progress") or {}
        yt = task.get("youtube_progress") or {}
        if (hf.get("total", 0) == 0 or yt.get("total", 0) == 0) and task.get("run_id"):
            progress = dispatcher.progress_markers(task["run_id"])
            if progress:
                for key, value in progress.items():
                    if value.get("total", 0) > 0 and task.get(key) != value:
                        task[key] = value
                        changed = True
    return queue, changed

