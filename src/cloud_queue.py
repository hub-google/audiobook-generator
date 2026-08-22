"""Durable GitHub-backed queue for unattended audiobook production."""

from __future__ import annotations

import base64
import copy
import json
import re
import time
import uuid
from datetime import datetime, timezone

import requests


QUEUE_SCHEMA_VERSION = 2
BLOCKING_STATES = {"dispatching", "running", "waiting_retry", "needs_attention", "canceling"}
TERMINAL_STATES = {"completed", "stopped", "interrupted"}


ACTIVE_EXECUTION_STATES = {"dispatching", "running", "canceling"}
NON_ACTIVE_STATES = {"completed", "stopped", "interrupted", "needs_attention", "paused", "waiting_retry"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def empty_queue():
    return {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "revision": 0,
        "updated_at": utc_now(),
        "queue": [],
        "completed": [],
    }


def is_task_active(task):
    if not isinstance(task, dict):
        return False
    if task.get("status") in ACTIVE_EXECUTION_STATES:
        return True
    if task.get("run_id") and not task.get("run_conclusion") and task.get("status") not in NON_ACTIVE_STATES:
        return True
    return False


def is_task_blocking(task):
    """Whether a task prevents dispatch, including terminal failures awaiting intervention."""
    return isinstance(task, dict) and (
        task.get("status") in BLOCKING_STATES or is_task_active(task)
    )


def normalize_queue(value):
    queue = copy.deepcopy(value) if isinstance(value, dict) else empty_queue()
    queue.setdefault("revision", 0)
    # Schema v1 stored pending and completed books in one ranked list.  Split
    # them during every read so old cloud state is safe before it is persisted.
    if "tasks" in queue:
        legacy = queue.pop("tasks")
        if not isinstance(legacy, list):
            raise ValueError("queue tasks must be a list")
        queue["queue"] = [item for item in legacy if item.get("status") != "completed"]
        queue["completed"] = [item for item in legacy if item.get("status") == "completed"]
    queue.setdefault("queue", [])
    queue.setdefault("completed", [])
    if not isinstance(queue["queue"], list) or not isinstance(queue["completed"], list):
        raise ValueError("queue and completed must be lists")

    # Repair misplaced records without ever allowing completed work to occupy
    # a scheduling position.
    misplaced = [item for item in queue["queue"] if item.get("status") == "completed"]
    queue["queue"] = [item for item in queue["queue"] if item.get("status") != "completed"]
    queue["completed"].extend(misplaced)
    queue["queue"].sort(key=lambda item: (
        0 if is_task_blocking(item) else 1,
        int(item.get("position", 10**9)),
        item.get("created_at", ""),
    ))
    for position, task in enumerate(queue["queue"], 1):
        task["position"] = position
    seen = set()
    completed = []
    pending_ids = {item.get("task_id") for item in queue["queue"]}
    for task in queue["completed"]:
        task.pop("position", None)
        task["status"] = "completed"
        task_id = task.get("task_id")
        if task_id not in pending_ids and task_id not in seen:
            completed.append(task)
            seen.add(task_id)
    queue["completed"] = completed
    queue["schema_version"] = QUEUE_SCHEMA_VERSION
    return queue


def format_chapter_label(start_chap, end_chap, excluded_chapters=None, renumber_selected=False):
    start = int(start_chap)
    end = int(end_chap) if end_chap is not None else 999999
    if end >= 999999:
        return f"Ch{start}-全部"
    excluded = {int(x) for x in (excluded_chapters or []) if start <= int(x) <= end}
    total_selected = max(0, (end - start + 1) - len(excluded))

    if renumber_selected:
        if excluded:
            return f"Ch1-{total_selected} (共{total_selected}章)"
        return f"Ch1-{total_selected}"
    else:
        if excluded:
            return f"Ch{start}-{end} (實做{total_selected}章)"
        return f"Ch{start}-{end}"


def new_task(catalog_url, book_title="", start_chapter=1, end_chapter=None, excluded_chapters=None,
             renumber_selected=False, duplicate_chapter_count=None, chapter_title_overrides=None):
    now = utc_now()
    return {
        "task_id": f"book-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8]}",
        "position": 0,
        "book_title": str(book_title or "待解析"),
        "catalog_url": str(catalog_url).strip(),
        "start_chapter": int(start_chapter),
        "end_chapter": int(end_chapter) if end_chapter is not None else None,
        "excluded_chapters": sorted({int(value) for value in (excluded_chapters or [])}),
        "renumber_selected": bool(renumber_selected),
        "duplicate_chapter_count": (
            int(duplicate_chapter_count) if duplicate_chapter_count is not None else None
        ),
        "chapter_title_overrides": {
            str(int(key)): str(value) for key, value in (chapter_title_overrides or {}).items()
            if str(key).isdigit() and str(value).strip()
        },
        "status": "queued",
        "run_id": None,
        "run_attempt": 0,
        "execution_generation": 1,
        "retry_requested_at": None,
        "run_history": [],
        "retry_at": None,
        "reason": None,
        "hf_progress": {"completed": 0, "total": 0},
        "youtube_progress": {"completed": 0, "total": 0},
        "created_at": now,
        "updated_at": now,
    }


def add_tasks(queue, tasks, position=None):
    queue = normalize_queue(queue)
    insert_at = len(queue["queue"]) if position is None else max(0, min(len(queue["queue"]), int(position) - 1))
    queue["queue"][insert_at:insert_at] = [copy.deepcopy(task) for task in tasks]
    return touch(queue)


def move_task(queue, task_id, position):
    queue = normalize_queue(queue)
    index = next((i for i, item in enumerate(queue["queue"]) if item.get("task_id") == task_id), None)
    if index is None:
        raise KeyError(task_id)
    task = queue["queue"].pop(index)
    if is_task_active(task):
        raise ValueError("an active task cannot be reordered")
    target = max(0, min(len(queue["queue"]), int(position) - 1))
    queue["queue"].insert(target, task)
    return touch(queue)


def move_tasks(queue, task_ids, delta):
    """Move a multi-selection one row while preserving its relative order."""
    queue = normalize_queue(queue)
    selected_ids = {str(task_id) for task_id in task_ids}
    if not selected_ids or int(delta) == 0:
        return queue
    missing = selected_ids - {str(item.get("task_id")) for item in queue["queue"]}
    if missing:
        raise KeyError(next(iter(missing)))
    selected = [item for item in queue["queue"] if str(item.get("task_id")) in selected_ids]
    if any(is_task_active(item) for item in selected):
        raise ValueError("an active task cannot be reordered")

    tasks = queue["queue"]
    if int(delta) < 0:
        for index in range(1, len(tasks)):
            if (str(tasks[index].get("task_id")) in selected_ids and
                    str(tasks[index - 1].get("task_id")) not in selected_ids):
                tasks[index - 1], tasks[index] = tasks[index], tasks[index - 1]
    else:
        for index in range(len(tasks) - 2, -1, -1):
            if (str(tasks[index].get("task_id")) in selected_ids and
                    str(tasks[index + 1].get("task_id")) not in selected_ids):
                tasks[index], tasks[index + 1] = tasks[index + 1], tasks[index]
    return touch(queue)


def update_task(queue, task_id, **changes):
    queue = normalize_queue(queue)
    task = next((item for item in queue["queue"] if item.get("task_id") == task_id), None)
    if task is None:
        raise KeyError(task_id)
    task.update(changes)
    task["updated_at"] = utc_now()
    return touch(queue)


def update_task_chapters(queue, task_id, start_chapter, end_chapter, excluded_chapters=None,
                         requeue_after_cancel=False, renumber_selected=False,
                         duplicate_chapter_count=None, chapter_title_overrides=None):
    """Persist an edited chapter plan and optionally restart after cancellation."""
    start = int(start_chapter)
    end = int(end_chapter)
    if start < 1 or end < start:
        raise ValueError("章節範圍無效")
    excluded = sorted({int(value) for value in (excluded_chapters or []) if start <= int(value) <= end})
    changes = {
        "start_chapter": start, "end_chapter": end,
        "excluded_chapters": excluded,
        "renumber_selected": bool(renumber_selected),
    }
    if duplicate_chapter_count is not None:
        changes["duplicate_chapter_count"] = int(duplicate_chapter_count)
    if chapter_title_overrides is not None:
        changes["chapter_title_overrides"] = {
            str(int(key)): str(value) for key, value in chapter_title_overrides.items()
            if str(key).isdigit() and str(value).strip()
        }
    if requeue_after_cancel:
        changes.update({"status": "canceling", "reason": "chapter_plan_updated", "requeue_after_edit": True})
    return update_task(queue, task_id, **changes)


def mark_task_interrupted(queue, task_id, reason="run_cancelled", conclusion="cancelled", ended_at=None):
    """Keep a book task but release its cancelled or missing Actions run."""
    queue = normalize_queue(queue)
    task = next((item for item in queue["queue"] if item.get("task_id") == task_id), None)
    if task is None:
        raise KeyError(task_id)
    run_id = task.get("run_id")
    history = list(task.get("run_history") or [])
    if run_id and not any(item.get("run_id") == run_id for item in history):
        history.append({
            "run_id": run_id,
            "conclusion": conclusion,
            "ended_at": ended_at or utc_now(),
        })
    task.update({
        "status": "interrupted",
        "reason": reason,
        "retry_at": None,
        "run_conclusion": conclusion,
        "run_completed_at": ended_at or utc_now(),
        "run_history": history,
        "updated_at": utc_now(),
    })
    return touch(queue)


from datetime import datetime, timedelta, timezone


def mark_task_waiting_retry(queue, task_id, reason="run_failure", conclusion="failure", ended_at=None, retry_at=None):
    """Mark a task for automatic retry within 2 hours."""
    queue = normalize_queue(queue)
    task = next((item for item in queue["queue"] if item.get("task_id") == task_id), None)
    if task is None:
        raise KeyError(task_id)
    run_id = task.get("run_id")
    history = list(task.get("run_history") or [])
    if run_id and not any(item.get("run_id") == run_id for item in history):
        history.append({
            "run_id": run_id,
            "conclusion": conclusion,
            "ended_at": ended_at or utc_now(),
        })
    default_retry = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    task.update({
        "status": "waiting_retry",
        "reason": reason,
        "retry_at": retry_at or task.get("retry_at") or default_retry,
        "run_conclusion": conclusion,
        "run_completed_at": ended_at or utc_now(),
        "run_history": history,
        "updated_at": utc_now(),
    })
    return touch(queue)


def mark_task_needs_attention(queue, task_id, reason="run_failed", conclusion="failure", ended_at=None, retry_at=None):
    """Legacy alias: ensure all failures transition to waiting_retry with guaranteed retry deadline."""
    return mark_task_waiting_retry(queue, task_id, reason=reason, conclusion=conclusion, ended_at=ended_at, retry_at=retry_at)


def delete_task(queue, task_id):
    queue = normalize_queue(queue)
    queue["queue"] = [item for item in queue["queue"] if item.get("task_id") != task_id]
    return touch(queue)


def requeue_task_after_active(queue, task_id, active_id=None):
    """Requeue an interrupted book immediately behind the active book.

    A cancelled Actions run is only one execution attempt.  The durable book
    task remains in the queue until the user explicitly deletes it.
    """
    queue = normalize_queue(queue)
    index = next((i for i, item in enumerate(queue["queue"]) if item.get("task_id") == task_id), None)
    if index is None:
        raise KeyError(task_id)
    task = queue["queue"][index]
    if is_task_active(task):
        raise ValueError("執行中的任務必須先取消目前 Run，再重新排程")

    if not active_id:
        active_id = next(
            (item.get("task_id") for item in queue["queue"]
             if item.get("task_id") != task_id and is_task_active(item)),
            None,
        )
    old_run_id = task.get("run_id")
    history = list(task.get("run_history") or [])
    if old_run_id and not any(item.get("run_id") == old_run_id for item in history):
        history.append({
            "run_id": old_run_id,
            "conclusion": task.get("run_conclusion") or "cancelled",
            "ended_at": task.get("run_completed_at") or utc_now(),
        })
    task.update({
        "status": "queued",
        "run_id": None,
        "run_attempt": 0,
        "run_conclusion": None,
        "run_completed_at": None,
        "reason": None,
        "retry_at": None,
        "retry_requested_at": utc_now(),
        "execution_generation": int(task.get("execution_generation") or 1) + 1,
        "run_history": history,
    })

    task = queue["queue"].pop(index)
    if active_id and any(item.get("task_id") == active_id for item in queue["queue"]):
        active_index = next(i for i, item in enumerate(queue["queue"]) if item.get("task_id") == active_id)
        queue["queue"].insert(active_index + 1, task)
    else:
        queue["queue"].insert(0, task)
    return touch(queue)


def current_task(queue):
    queue = normalize_queue(queue)
    return next((item for item in queue["queue"] if is_task_blocking(item)), None)


def next_task(queue):
    if current_task(queue):
        return None
    return next((item for item in normalize_queue(queue)["queue"] if item.get("status") == "queued"), None)


def touch(queue):
    if not isinstance(queue, dict) or not isinstance(queue.get("queue"), list):
        raise ValueError("invalid queue")
    queue.setdefault("completed", [])
    newly_completed = [t for t in queue["queue"] if t.get("status") == "completed"]
    active = [t for t in queue["queue"] if t.get("status") != "completed" and is_task_blocking(t)]
    non_active = [t for t in queue["queue"] if t.get("status") != "completed" and not is_task_blocking(t)]
    queue["queue"] = active + non_active
    existing_ids = {t.get("task_id") for t in queue["completed"]}
    for task in newly_completed:
        task.pop("position", None)
        if task.get("task_id") not in existing_ids:
            queue["completed"].append(task)
            existing_ids.add(task.get("task_id"))
    for position, task in enumerate(queue["queue"], 1):
        task["position"] = position
    queue["revision"] = int(queue.get("revision", 0)) + 1
    queue["updated_at"] = utc_now()
    return queue


class QueueConflict(RuntimeError):
    pass


class GitHubQueueStore:
    """A queue.json on a dedicated Git branch, updated with the Contents API SHA guard."""

    def __init__(self, repo, token, branch="automation-state", path="audiobook-queue.json", timeout=20):
        self.repo = repo
        self.branch = branch
        self.path = path
        self.timeout = timeout
        self.base = f"https://api.github.com/repos/{repo}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method, url, **kwargs):
        response = requests.request(method, url, headers=self.headers, timeout=self.timeout, **kwargs)
        return response

    def ensure_branch(self):
        ref_url = f"{self.base}/git/ref/heads/{self.branch}"
        response = self._request("GET", ref_url)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise RuntimeError(f"GitHub branch check failed ({response.status_code}): {response.text}")
        repo_info = self._request("GET", self.base)
        repo_info.raise_for_status()
        default_branch = repo_info.json()["default_branch"]
        source = self._request("GET", f"{self.base}/git/ref/heads/{default_branch}")
        source.raise_for_status()
        created = self._request("POST", f"{self.base}/git/refs", json={
            "ref": f"refs/heads/{self.branch}", "sha": source.json()["object"]["sha"],
        })
        if created.status_code not in (201, 422):
            raise RuntimeError(f"GitHub branch creation failed ({created.status_code}): {created.text}")

    def load(self):
        self.ensure_branch()
        response = self._request("GET", f"{self.base}/contents/{self.path}", params={"ref": self.branch})
        if response.status_code == 404:
            return empty_queue(), None
        if response.status_code != 200:
            raise RuntimeError(f"GitHub queue read failed ({response.status_code}): {response.text}")
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        return normalize_queue(json.loads(content)), payload["sha"]

    def save(self, queue, sha=None, message="Update audiobook production queue"):
        self.ensure_branch()
        data = normalize_queue(queue)
        body = {
            "message": message,
            "branch": self.branch,
            "content": base64.b64encode((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        response = self._request("PUT", f"{self.base}/contents/{self.path}", json=body)
        if response.status_code in (409, 422):
            raise QueueConflict("queue changed remotely; reload before saving")
        if response.status_code not in (200, 201):
            raise RuntimeError(f"GitHub queue write failed ({response.status_code}): {response.text}")
        return response.json()["content"]["sha"]

    def mutate(self, callback, message, attempts=4):
        for attempt in range(attempts):
            queue, sha = self.load()
            updated = callback(queue)
            try:
                self.save(updated, sha=sha, message=message)
                return normalize_queue(updated)
            except QueueConflict:
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.5 * (attempt + 1))


def task_id_from_run_name(name):
    match = re.search(r"(book-\d{8}-[0-9a-f]{8})", str(name or ""), re.I)
    return match.group(1) if match else None
