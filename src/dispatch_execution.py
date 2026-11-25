"""Queue reservation and Actions dispatch, kept separate from reconciliation."""

def dispatch_next(dispatcher, queue, reservation_sha):
    from .queue_dispatcher import (
        _AUTO_TASK, QueueConflict, copy, next_task, get_book_profile,
        profile_snapshot, format_chapter_label, update_task, parse_time,
        task_id_from_run_name, datetime, timezone, base64, json, time,
    )
    # The normal run path already has the exact SHA from load/reconcile.
    # Standalone callers must verify their snapshot before reserving.
    if reservation_sha is _AUTO_TASK:
        latest, reservation_sha = dispatcher.store.load()
        if latest != queue:
            raise QueueConflict("queue changed before dispatch reservation")
    queue = copy.deepcopy(queue)
    active_runs = [r for r in dispatcher.runs()
                   if r.get("status") != "completed"
                   and not dispatcher.is_authoritatively_completed_run(r)]
    active_cover_runs = [r for r in dispatcher.cover_runs()
                         if r.get("status") != "completed"
                         and not dispatcher.is_authoritatively_completed_run(r)]
    if active_runs or active_cover_runs:
        first_run = (active_runs or active_cover_runs)[0]
        return queue, f"GitHub Run {first_run.get('id')} ({first_run.get('display_title') or first_run.get('name')}) is already active. Will not dispatch another task."

    task = next_task(queue)
    if not task:
        return queue, "No queued task is eligible."
    task_id = task["task_id"]
    profiles, _ = dispatcher.profile_store.load()
    profile_id, profile = get_book_profile(
        profiles, task.get("catalog_url") or "", task.get("book_title") or "",
    )
    snapshot = profile_snapshot(profile_id, profile)
    if task.get("workflow_phase") == "processing" and task.get("scrape_run_id"):
        review = task.get("ad_review") or {}
        if review.get("status") != "approved":
            raise RuntimeError("Processing requires approved preflight review")
        snapshot = profile_snapshot(profile_id, {**profile, "cleaner_remove_patterns": review["remove_patterns"]})
    snapshot["catalog_identity"] = task.get("catalog_identity") or ""
    # Existing tasks created before book profiles retain their edited titles
    # until the first explicit profile save migrates them.
    if not snapshot.get("chapter_title_overrides") and task.get("chapter_title_overrides"):
        snapshot["chapter_title_overrides"] = dict(task.get("chapter_title_overrides") or {})
    if (not snapshot.get("chapter_normalized_number_overrides") and
            task.get("chapter_normalized_number_overrides")):
        snapshot["chapter_normalized_number_overrides"] = dict(
            task.get("chapter_normalized_number_overrides") or {}
        )
    task["book_profile_id"] = profile_id
    task["profile_snapshot"] = snapshot
    stages = task.get("stages") or {}
    is_processing = task.get("workflow_phase") == "processing"
    need_scrape = not is_processing and (stages.get("scrape") or {}).get("status") != "completed"
    need_cover = not is_processing and (stages.get("cover") or {}).get("status") != "completed"
    # A reviewed TXT Run is an input snapshot, never a processing resume
    # source.  Only the task's own previous processing Run may supply
    # worker/video checkpoints; legacy tasks without a TXT snapshot retain
    # the older failed-artifact discovery as a compatibility fallback.
    source_run_id = None
    if is_processing:
        source_run_id = task.get("processing_run_id") or None
        if not source_run_id and not task.get("scrape_run_id"):
            source_run_id = dispatcher.select_artifact_source_run_id(
                queue, profile_id, task_id, current_task=task,
            )
    task["artifact_source_run_id"] = source_run_id
    phase = "processing" if is_processing else "preflight"
    for name, needed in (("scrape", need_scrape), ("cover", need_cover)):
        if needed:
            stages.setdefault(name, {}).update(status="dispatching", run_id=None,
                dispatched_at=datetime.now(timezone.utc).isoformat())
    task.update({"status": "dispatching", "workflow_phase": phase, "reason": None, "retry_at": None, "dispatched_at": datetime.now(timezone.utc).isoformat()})
    queue = update_task(
        queue, task_id,
        book_profile_id=profile_id,
        profile_snapshot=snapshot,
        artifact_source_run_id=source_run_id,
        status="dispatching",
        workflow_phase=phase,
        stages=stages,
        reason=None,
        retry_at=None,
        dispatched_at=task["dispatched_at"],
    )
    dispatcher.store.save(queue, sha=reservation_sha, message=f"Reserve audiobook task {task_id}")

    start_int = int(task.get("start_chapter") or 1)
    end_int = int(task.get("end_chapter") or 999999)
    excluded = task.get("excluded_chapters") or []
    renumber = bool(task.get("renumber_selected"))
    chapter_label = format_chapter_label(start_int, end_int, excluded_chapters=excluded, renumber_selected=renumber)

    common_inputs = {
        "book_title": task.get("book_title") or "待解析書名",
        "chapter_label": chapter_label,
        "queue_task_id": task_id,
        "resume_source_run_id": str(source_run_id or ""),
        "catalog_url": task["catalog_url"],
        "start_chap": str(start_int),
        "end_chap": str(end_int),
        "exclude_chapters": ",".join(str(value) for value in sorted(task.get("excluded_chapters") or [])),
        "renumber_selected": "true" if renumber else "false",
        "publish_at": str(task.get("publish_at") or ""),
        "chapter_title_overrides_b64": base64.b64encode(
            json.dumps(snapshot.get("chapter_title_overrides") or {}, ensure_ascii=False).encode("utf-8")
        ).decode("ascii"),
        "chapter_order_b64": base64.b64encode(
            json.dumps(task.get("chapter_order") or []).encode("utf-8")
        ).decode("ascii"),
        "book_profile_snapshot_b64": base64.b64encode(
            json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        ).decode("ascii"),
        "zip_password": "",
    }
    inputs = dict(common_inputs, execution_phase=("resume_processing" if source_run_id else "processing") if is_processing else "scrape_only",
                  # A failed/pending TXT stage is being regenerated and cannot
                  # also be used as the reviewed snapshot source for that run.
                  scrape_source_run_id=str(task.get("scrape_run_id") or "") if not need_scrape else "",
                  cover_source_run_id=str(task.get("cover_run_id") or ""))
    cover_inputs = {key: value for key, value in common_inputs.items() if key not in {
        "chapter_label", "resume_source_run_id", "zip_password", "publish_at",
    }}
    dispatch_requested_at = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        if need_scrape or is_processing:
            dispatcher.request("POST", "/actions/workflows/audiobook.yml/dispatches", json={"ref": "master", "inputs": inputs})
        if need_cover:
            dispatcher.request("POST", "/actions/workflows/cover-preflight.yml/dispatches", json={"ref": "master", "inputs": cover_inputs})
    except Exception as error:
        latest, latest_sha = dispatcher.store.load()
        # Preserve the successfully submitted sibling when the second
        # dispatch fails; only the failed dispatch needs another attempt.
        failed_stage = "cover" if need_cover and (need_scrape or not is_processing) else "processing"
        if need_scrape and "/audiobook.yml/" in str(error):
            failed_stage = "scrape"
        stages.setdefault(failed_stage, {}).update(status="failed", reason=str(error))
        failed = update_task(latest, task_id, status="needs_attention", stages=stages, reason=f"dispatch_failed: {error}")
        dispatcher.store.save(failed, sha=latest_sha, message=f"Record dispatch failure for {task_id}")
        raise
    # workflow_dispatch returns 204 without a Run ID. Match the unique
    # task ID embedded in run-name so the GUI can open/cancel the exact Run
    # immediately instead of waiting for the next 15-minute reconciliation.
    run_id = None if (need_scrape or is_processing) else task.get("scrape_run_id")
    cover_run_id = None if need_cover else task.get("cover_run_id")
    # workflow_dispatch does not return a Run id. GitHub indexing is often
    # a few seconds behind the accepted dispatch, so confirm every Run that
    # was requested instead of recording a successful dispatch with null IDs.
    for delay in dispatcher.dispatch_discovery_delays:
        if delay:
            time.sleep(delay)
        if need_scrape or is_processing:
            for run in dispatcher.runs():
                created_at = parse_time(run.get("created_at"))
                if (task_id_from_run_name(run.get("display_title") or run.get("name")) == task_id
                        and created_at is not None and created_at >= dispatch_requested_at):
                    run_id = int(run["id"]); break
        if need_cover:
            for run in dispatcher.cover_runs():
                created_at = parse_time(run.get("created_at"))
                if (task_id_from_run_name(run.get("display_title") or run.get("name")) == task_id
                        and created_at is not None and created_at >= dispatch_requested_at):
                    cover_run_id = int(run["id"]); break
        primary_ready = not (need_scrape or is_processing) or bool(run_id)
        cover_ready = not need_cover or bool(cover_run_id)
        if primary_ready and cover_ready:
            break
    if is_processing and run_id:
        stages.setdefault("processing", {}).update({"run_id": run_id, "status": "running", "run_attempt": int((stages.get("processing") or {}).get("run_attempt") or 0) + 1})
        task.update({"status": "processing", "run_id": run_id, "processing_run_id": run_id, "run_attempt": 1})
        latest, latest_sha = dispatcher.store.load()
        running = update_task(latest, task_id, status="processing", run_id=run_id,
                              processing_run_id=run_id, stages=stages, run_attempt=1)
        dispatcher.store.save(running, sha=latest_sha, message=f"Attach processing Run {run_id} to {task_id}")
    elif run_id or cover_run_id:
        stages = task.setdefault("stages", {})
        if need_scrape:
            stages.setdefault("scrape", {}).update({"run_id": run_id, "status": "running" if run_id else "dispatching", "run_attempt": int((stages.get("scrape") or {}).get("run_attempt") or 0) + 1})
        if need_cover:
            stages.setdefault("cover", {}).update({"run_id": cover_run_id, "status": "running" if cover_run_id else "dispatching", "run_attempt": int((stages.get("cover") or {}).get("run_attempt") or 0) + 1})
        task.update({"status": "preparing_assets", "run_id": run_id, "scrape_run_id": run_id, "cover_run_id": cover_run_id, "run_attempt": 1})
        latest, latest_sha = dispatcher.store.load()
        running = update_task(latest, task_id, status="preparing_assets", run_id=run_id,
                              scrape_run_id=run_id, cover_run_id=cover_run_id,
                              stages=stages, run_attempt=1)
        dispatcher.store.save(running, sha=latest_sha, message=f"Attach preflight Runs to {task_id}")
    else:
        # The durable record remains reserved as dispatching/preparing.
        # GitHub can take a few seconds to index workflow_dispatch; putting
        # the task back in queued here would allow a duplicate dispatch.
        queue = update_task(
            queue, task_id,
            status="dispatching" if is_processing else "preparing_assets",
        )
    if is_processing:
        return queue, f"Dispatched {task.get('book_title')} ({task_id}) processing Run {run_id or 'pending'} from TXT Run {task.get('scrape_run_id')}."
    return queue, f"Dispatched {task.get('book_title')} ({task_id}) as TXT Run {run_id or 'pending'} and cover Run {cover_run_id or 'pending'}."

