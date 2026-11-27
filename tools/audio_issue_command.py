"""Authorized GitHub Issues → Actions bridge for the static AUDIO web GUI.

The browser never receives a repository credential. Each operation is confirmed
by the repo collaborator on github.com; GITHUB_TOKEN is only available in Actions.
"""
from __future__ import annotations
import base64
import gzip
import json
import tempfile
from pathlib import Path
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from src.cloud_queue import (
    GitHubQueueStore, new_task, add_tasks, move_tasks, update_task,
    mark_tasks_completed, move_tasks_to_pending, requeue_task_after_active,
    update_task_chapters, delete_task, start_reviewed_processing,
    confirm_preflight_review, utc_now, normalize_queue
)
from src.book_profiles import GitHubBookProfileStore, update_book_profile
from src.catalog_parser import parse_catalog

OPEN = "<!-- AUDIO-COMMAND-V1:"
CLOSE = ":AUDIO-COMMAND-V1 -->"
ALLOWED = {
    "enqueue", "batch_add", "edit", "move", "status", "delete",
    "complete", "restore", "requeue", "cancel", "review", "dispatch",
    "preview", "ping", "manual_cover", "export_artifacts",
}
TASK_FIELDS = {
    "book_title", "start_chapter", "end_chapter", "excluded_chapters",
    "renumber_selected", "chapter_title_overrides", "chapter_normalized_number_overrides",
    "chapter_order", "publish_at", "duplicate_chapter_count",
    "duplicate_detection",
    "catalog_identity",
}
WRITE_ROLES = {"admin", "maintain", "write"}


def checked_detection(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Duplicate detection conditions must be an object")
    result = {
        "use_normalized_number": bool(value.get("use_normalized_number", True)),
        "use_chapter_name": bool(value.get("use_chapter_name", True)),
        "use_number_and_name": bool(value.get("use_number_and_name", False)),
    }
    if not any(result.values()):
        raise ValueError("Select at least one duplicate detection condition")
    return result



def decode_command(body):
    if not isinstance(body, str):
        raise ValueError("Missing issue body")
    match = re.search(re.escape(OPEN) + r"([A-Za-z0-9_-]+)" + re.escape(CLOSE), body)
    if not match:
        raise ValueError("No valid AUDIO operation in issue")
    encoded = match.group(1)
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(raw) > 300_000:
        raise ValueError("Command payload too large")
    try:
        data = json.loads(gzip.decompress(raw))
    except (gzip.BadGzipFile, OSError):
        data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unsupported command version")
    if data.get("op") not in ALLOWED:
        raise ValueError("Unsupported command operation")
    if len(json.dumps(data, ensure_ascii=False)) > 400_000:
        raise ValueError("Command too large")
    return data


def api(method, path, token, repo, **kwargs):
    response = requests.request(
        method, f"https://api.github.com/repos/{repo}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=40, **kwargs
    )
    if response.status_code >= 400:
        raise RuntimeError(f"GitHub action failed: HTTP {response.status_code}")
    if response.status_code == 204:
        return {}
    return response.json()


def authorized(actor, token, repo):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37})", str(actor or "")):
        return False
    try:
        info = api("GET", f"collaborators/{actor}/permission", token, repo)
    except RuntimeError:
        return False
    return info.get("permission") in WRITE_ROLES


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"book-\d{8}-[0-9a-f]{8}", value):
        raise ValueError("Invalid task ID")
    return value


def valid_ids(value):
    ids = value if isinstance(value, list) else [value]
    if not ids or len(ids) != len(set(map(str, ids))):
        raise ValueError("Invalid task selection")
    return [valid_id(v) for v in ids]


def valid_url(url):
    if not isinstance(url, str):
        raise ValueError("Invalid catalog URL")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("Only HTTP(S) catalog URLs are supported")
    from src.sources import resolve_source
    resolve_source(url)
    return url.strip()


def valid_chapters(entry, catalog):
    start = int(entry.get("start_chapter") or 1)
    end = int(entry.get("end_chapter") or len(catalog["chapter_titles"]))
    total = len(catalog["chapter_titles"])
    if not (1 <= start <= end <= total):
        raise ValueError("Chapter range exceeds live catalog")
    excluded = sorted(set(map(int, entry.get("excluded_chapters") or [])))
    if any(n < start or n > end for n in excluded):
        raise ValueError("Excluded chapter outside selected range")
    if end - start + 1 == len(excluded):
        raise ValueError("Cannot exclude every selected chapter")
    return start, end, excluded


def build_book(entry, issue_number):
    url = valid_url(entry.get("catalog_url"))
    parsed = parse_catalog(url)
    if not parsed.get("success") or not parsed.get("total_chapters"):
        raise RuntimeError("Live catalog parsing failed")
    start, end, excluded = valid_chapters(entry, parsed)
    selected = set(range(start, end + 1)) - set(excluded)
    order = [int(n) for n in (entry.get("chapter_order") or [])]
    if order and (len(order) != len(selected) or set(order) != selected):
        raise ValueError("Chapter order must include every selected chapter once")
    task = new_task(
        catalog_url=parsed["catalog_url"],
        book_title=str(entry.get("book_title") or parsed["book_title"])[:160],
        start_chapter=start, end_chapter=end,
        excluded_chapters=excluded,
        renumber_selected=bool(entry.get("renumber_selected", True)),
        duplicate_chapter_count=parsed.get("duplicate_chapter_count"),
        chapter_title_overrides=entry.get("chapter_title_overrides") or {},
        chapter_normalized_number_overrides=entry.get("chapter_normalized_number_overrides") or {},
        chapter_order=order or sorted(selected),
        catalog_identity=parsed["catalog_identity"],
        publish_at=entry.get("publish_at"),
    )
    task["request_issue"] = issue_number
    detection = checked_detection(entry.get("duplicate_detection"))
    if detection is not None:
        task["duplicate_detection"] = detection
    return task


def apply_command(command, store, profiles, issue_number, token, repo):
    operation = command["op"]
    values = command.get("data") or {}
    if not isinstance(values, dict):
        raise ValueError("Command data must be an object")
    if operation == "dispatch":
        return "Submitted dispatcher", True
    if operation == "ping":
        return "GitHub collaborator verified; no queue change", False
    if operation == "export_artifacts":
        ids = valid_ids(values.get("task_ids"))
        state, _ = store.load()
        tasks = {t["task_id"]: t for t in state.get("queue", []) + state.get("completed", [])}
        if set(ids) - set(tasks):
            raise ValueError("One or more selected tasks no longer exist")
        runs = []
        for task_id in ids:
            task = tasks[task_id]
            for field in ("run_id", "scrape_run_id", "cover_run_id", "processing_run_id"):
                run_id = task.get(field)
                if str(run_id or "").isdigit() and int(run_id) > 0:
                    runs.append(int(run_id))
            for item in task.get("run_history") or []:
                run_id = item.get("run_id")
                if str(run_id or "").isdigit() and int(run_id) > 0:
                    runs.append(int(run_id))
        runs = list(dict.fromkeys(runs))
        if not runs:
            raise ValueError("Selected books have no GitHub Run artifacts")
        api("POST", "actions/workflows/audio-export-artifacts.yml/dispatches", token, repo,
            json={"ref": "main", "inputs": {
                "source_run_ids": ",".join(str(n) for n in runs),
                "request_id": f"issue-{issue_number}",
            }})
        return f"Requested {len(runs)} Runs for batch artifact export", False
    if operation == "preview":
        request_id = str(values.get("request_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", request_id):
            raise ValueError("Invalid preview request ID")
        url = valid_url(values.get("catalog_url"))
        api("POST", "actions/workflows/web-catalog-preview.yml/dispatches", token, repo,
            json={"ref": "main", "inputs": {"catalog_url": url, "request_id": request_id}})
        return "Started catalog preview " + request_id, False

    # Parse new books before queue mutation; a failed book never inserts a partial batch.
    new_books = []
    if operation in {"enqueue", "batch_add"}:
        entries = values.get("books") or []
        if not isinstance(entries, list) or not entries:
            raise ValueError("No books requested")
        new_books = [build_book(entry, issue_number) for entry in entries]

    ids = []
    if operation in {"edit", "move", "status", "delete", "complete", "restore", "requeue", "cancel", "review"}:
        ids = valid_ids(values.get("task_ids") or values.get("task_id"))
    if operation in {"edit", "requeue", "review", "cancel"} and len(ids) != 1:
        raise ValueError("This operation requires one task")

    def apply_mutation(q):
        q = normalize_queue(q)
        if any(t.get("request_issue") == issue_number for t in q.get("queue", []) + q.get("completed", [])):
            return q
        targets = [t for t in q["queue"] if t.get("task_id") in ids]
        if operation not in {"enqueue", "batch_add", "restore"} and len(targets) != len(ids):
            raise ValueError("Selected task no longer exists in pending queue")
        if operation in {"enqueue", "batch_add"}:
            return add_tasks(q, new_books)
        if operation == "move":
            return move_tasks(q, ids, int(values.get("delta", 0)))
        if operation == "status":
            status = values.get("status")
            if status not in {"queued", "paused"} or any(t.get("status") not in {"queued", "paused"} for t in targets):
                raise ValueError("Cannot change status of an active task")
            for task in targets:
                q = update_task(q, task["task_id"], status=status, reason=None, retry_at=None)
            return q
        if operation == "complete":
            if any(t["status"] not in {"queued", "paused", "interrupted", "stopped"} for t in targets):
                raise ValueError("Cannot complete an active task")
            return mark_tasks_completed(q, ids, conclusion="manual")
        if operation == "restore":
            existing = {t["task_id"] for t in q["completed"]}
            if set(ids) - existing:
                raise ValueError("Selected task is not completed")
            return move_tasks_to_pending(q, ids, status="paused")
        if operation == "delete":
            if any(t["status"] not in {"queued", "paused", "interrupted", "stopped"} for t in targets):
                raise ValueError("Cannot delete an active task")
            for task_id in ids:
                q = delete_task(q, task_id)
            return q
        if operation == "requeue":
            return requeue_task_after_active(q, ids[0])
        if operation == "edit":
            task = targets[0]
            if task["status"] not in {"queued", "paused", "interrupted", "stopped"}:
                raise ValueError("Cannot edit an active task")
            changes = values.get("changes") or {}
            if not isinstance(changes, dict) or set(changes) - TASK_FIELDS:
                raise ValueError("Unsupported field in task edit")
            start = int(changes.get("start_chapter", task["start_chapter"]))
            end = int(changes.get("end_chapter", task["end_chapter"]))
            if start < 1 or end < start:
                raise ValueError("Invalid chapter range")
            excluded = changes.get("excluded_chapters", task.get("excluded_chapters") or [])
            excluded = sorted(set(map(int, excluded)))
            if any(n < start or n > end for n in excluded):
                raise ValueError("Excluded chapters outside range")
            selected = set(range(start,end+1))-set(excluded)
            order = changes.get("chapter_order", task.get("chapter_order") or [])
            if order and (len(order) != len(selected) or set(map(int,order)) != selected):
                raise ValueError("Incomplete chapter order")
            q = update_task_chapters(
                q, ids[0], start, end, excluded_chapters=excluded,
                renumber_selected=changes.get("renumber_selected",task.get("renumber_selected",False)),
                chapter_title_overrides=changes.get("chapter_title_overrides",task.get("chapter_title_overrides") or {}),
                chapter_normalized_number_overrides=changes.get("chapter_normalized_number_overrides",task.get("chapter_normalized_number_overrides") or {}),
                chapter_order=order, catalog_identity=task.get("catalog_identity"),
                publish_at=changes.get("publish_at") or "",
            )
            if "book_title" in changes:
                q = update_task(q,ids[0],book_title=str(changes["book_title"])[:160])
            if "duplicate_detection" in changes:
                q = update_task(q,ids[0],duplicate_detection=checked_detection(changes["duplicate_detection"]))
            return q
        if operation == "review":
            task = targets[0]
            if task.get("status") != "waiting_review":
                raise ValueError("Task not awaiting review")
            patterns = values.get("remove_patterns") or []
            if not isinstance(patterns,list):
                raise ValueError("Review patterns must be a list")
            review = {"status": "approved", "approved_at": utc_now(),
                      "remove_patterns": patterns}
            return confirm_preflight_review(
                q, ids[0],task.get("scrape_run_id"),task.get("cover_run_id"),review,
            ) if values.get("review_only") else start_reviewed_processing(
                confirm_preflight_review(q,ids[0],task.get("scrape_run_id"),task.get("cover_run_id"),review),ids[0]
            )
        if operation == "cancel":
            task = targets[0]
            run_ids = {v for v in (
                task.get("run_id"),task.get("scrape_run_id"),task.get("cover_run_id"),task.get("processing_run_id")
            ) if isinstance(v,int) and v>0}
            if not run_ids:
                raise ValueError("No GitHub Actions run exists to cancel")
            for run_id in run_ids:
                current = api("GET", f"actions/runs/{run_id}", token, repo)
                if current.get("status") == "completed":
                    continue
                try:
                    api("POST", f"actions/runs/{run_id}/cancel", token, repo)
                except RuntimeError:
                    api("POST", f"actions/runs/{run_id}/force-cancel", token, repo)
            # The dispatcher observes terminal Runs and settles the final state;
            # never claim stopped before GitHub confirms the cancellation.
            return update_task(q,ids[0],status="canceling",reason="user_cancelled")
        raise ValueError("Unsupported operation")

    def mutate(q):
        q = normalize_queue(q)
        applied = q.get("web_applied_issue_ids") or []
        if issue_number in applied:
            return q
        result = apply_mutation(q)
        result["web_applied_issue_ids"] = applied + [issue_number]
        return result

    # Save profile first for edits/reviews, as the desktop GUI persists the
    # same text-cleaner rules for future runs of this book.
    if operation in {"edit", "review"}:
        q, _ = store.load()
        task = next((t for t in q["queue"] if t["task_id"] == ids[0]),None)
        if task is None:
            raise ValueError("Task not found")
        profile_changes = (values.get("changes") or {}) if operation=="edit" else {}
        profiles.mutate(lambda p: update_book_profile(
            p,task["catalog_url"],profile_changes.get("book_title",task["book_title"]),
            cleaner_remove_patterns=values.get("remove_patterns") if operation=="review" else None,
            chapter_title_overrides=profile_changes.get("chapter_title_overrides") if operation=="edit" else None,
            chapter_normalized_number_overrides=profile_changes.get("chapter_normalized_number_overrides") if operation=="edit" else None,
            duplicate_detection=checked_detection(profile_changes.get("duplicate_detection")) if operation=="edit" else None,
        ),f"AUDIO issue #{issue_number} update profile")
    old, _ = store.load()
    already_applied = issue_number in (old.get("web_applied_issue_ids") or [])
    if already_applied:
        return f"Already processed issue #{issue_number}", False
    store.mutate(mutate,f"AUDIO issue #{issue_number}: {operation}")
    if operation in {"enqueue", "batch_add"}:
        for task in new_books:
            if task.get("duplicate_detection") or task.get("chapter_title_overrides") or task.get("chapter_normalized_number_overrides"):
                profiles.mutate(lambda p, t=task: update_book_profile(
                    p, t["catalog_url"], t["book_title"],
                    duplicate_detection=t.get("duplicate_detection"),
                    chapter_title_overrides=t.get("chapter_title_overrides"),
                    chapter_normalized_number_overrides=t.get("chapter_normalized_number_overrides"),
                ), f"AUDIO issue #{issue_number} persist chapter profile")
    return f"Completed {operation} ({len(ids) if ids else len(new_books)} book(s))",operation in {
        "enqueue","batch_add","requeue","status","review"
    }


def cover_attachment_url(issue_body):
    # Only accept native GitHub image uploads, not caller-controlled URLs.
    matches = re.findall(
        r"https://(?:github\.com/user-attachments/assets/[0-9a-fA-F-]{30,}"
        r"|user-images\.githubusercontent\.com/[A-Za-z0-9_./-]+)",
        issue_body or "",
    )
    if len(matches) != 1:
        raise ValueError("Attach exactly one image to the GitHub issue before submitting")
    return matches[0]


def upload_manual_cover_request(command, issue, token, repo):
    from src.book_profiles import book_profile_id
    from src.cover_assets import normalize_manual_cover, upload_github_cover
    data = command.get("data") or {}
    task_id = valid_id(data.get("task_id"))
    q, _ = GitHubQueueStore(repo, token).load()
    task = next((t for t in q.get("queue", []) if t.get("task_id") == task_id), None)
    if task is None:
        raise ValueError("Task is no longer pending")
    if task.get("workflow_phase") not in {"queued", "preflight", None} or task.get("status") in {"processing", "running", "completed"}:
        raise ValueError("Cannot replace cover during video processing")
    url = cover_attachment_url(issue.get("body", ""))
    response = requests.get(url, timeout=60, stream=True)
    response.raise_for_status()
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=128 * 1024):
        total += len(chunk)
        if total > 20_000_000:
            raise ValueError("Manual cover exceeds 20 MB")
        chunks.append(chunk)
    with tempfile.TemporaryDirectory(prefix="audio-cover-") as path:
        raw = Path(path) / "input_image"
        target = Path(path) / "master_cover.jpg"
        raw.write_bytes(b"".join(chunks))
        details = normalize_manual_cover(raw, target)
        profile_id = book_profile_id(task["catalog_url"])
        cloud = upload_github_cover(target, profile_id, repo, token)
    record = {"source": "manual_upload", **cloud, **details}
    GitHubBookProfileStore(repo, token).mutate(
        lambda data: update_book_profile(
            data, task["catalog_url"], task["book_title"], manual_cover=record,
        ), f"AUDIO manual cover issue {issue['number']}"
    )
    return "Manual cover verified and saved to book profile"


def execute(event, token, repo):
    issue = event.get("issue") or {}
    issue_id = issue.get("number")
    if not isinstance(issue_id, int):
        raise ValueError("No issue number")
    author = (issue.get("user") or {}).get("login")
    if not authorized(author, token, repo):
        raise PermissionError("Only a repository collaborator with write access can request AUDIO operations")
    command = decode_command(issue.get("body"))
    if command["op"] == "manual_cover":
        return upload_manual_cover_request(command, issue, token, repo)
    store = GitHubQueueStore(repo, token)
    profiles = GitHubBookProfileStore(repo, token)
    result, should_dispatch = apply_command(command,store,profiles,issue_id,token,repo)
    if should_dispatch:
        api("POST","actions/workflows/queue-dispatcher.yml/dispatches",token,repo,
            json={"ref":"main","inputs":{"force_retry":"false"}})
    return result


def main():
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"],encoding="utf-8") as f:
        event = json.load(f)
    issue = event.get("issue") or {}
    number = issue.get("number")
    if not isinstance(number,int):
        raise SystemExit("No triggering issue")
    try:
        message = execute(event,token,repo)
        api("POST",f"issues/{number}/comments",token,repo,
            json={"body":"✅ AUDIO 已完成請求："+message})
        api("PATCH",f"issues/{number}",token,repo,json={"state":"closed","state_reason":"completed"})
        print("AUDIO command processed",number)
    except Exception as exc:
        # Never put server-side API tokens or HTTP body in a public issue.
        safe = type(exc).__name__
        print("AUDIO command rejected",number,safe)
        api("POST",f"issues/{number}/comments",token,repo,
            json={"body":"❌ AUDIO 操作未完成（"+safe+"）。請核對身份、任務狀態和 Actions 日誌；沒有對外公布任何 Secret。"})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
