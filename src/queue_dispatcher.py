"""One-shot queue dispatcher used by queue-dispatcher.yml."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

import requests

try:
    from .cloud_queue import BLOCKING_STATES, GitHubQueueStore, current_task, next_task, task_id_from_run_name, update_task
except ImportError:
    from cloud_queue import BLOCKING_STATES, GitHubQueueStore, current_task, next_task, task_id_from_run_name, update_task


TRANSIENT_REASONS = {
    "quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "backendError", "otherError",
    "thumbnailRateLimit", "captionUploadFailed", "playlistInsertFailed", "publishFailed",
    "retryable YouTube API failure",
}


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class Dispatcher:
    def __init__(self, repo, token, branch="automation-state"):
        self.repo = repo
        self.token = token
        self.store = GitHubQueueStore(repo, token, branch=branch)
        self.headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.api = f"https://api.github.com/repos/{repo}"

    def request(self, method, path, **kwargs):
        response = requests.request(method, self.api + path, headers=self.headers, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API {method} {path} failed ({response.status_code}): {response.text}")
        return response

    def runs(self):
        return self.request("GET", "/actions/workflows/audiobook.yml/runs", params={"event": "workflow_dispatch", "per_page": 100}).json().get("workflow_runs", [])

    def retry_marker(self, run_id):
        try:
            command = ["gh", "run", "view", str(run_id), "--repo", self.repo, "--log-failed"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
            text = result.stdout + result.stderr
        except (OSError, subprocess.SubprocessError):
            return "otherError", datetime.now(timezone.utc) + timedelta(hours=2)
        reason_match = re.findall(r"reason=([^ |\r\n]+)", text)
        retry_match = re.findall(r"retry(?: after|_at=)([0-9T:.+\-Z]+)", text)
        reason = reason_match[-1] if reason_match else "otherError"
        retry_at = parse_time(retry_match[-1]) if retry_match else None
        return reason, retry_at or datetime.now(timezone.utc) + timedelta(hours=2)

    def progress_markers(self, run_id):
        try:
            result = subprocess.run(
                ["gh", "run", "view", str(run_id), "--repo", self.repo, "--log"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            text = result.stdout
        except (OSError, subprocess.SubprocessError):
            return None
        yt_parts = {int(value) for value in re.findall(r"\[API_UPLOAD_MARKER\] DONE \| Part (\d+)", text)}
        hf_parts = {int(value) for value in re.findall(r"\[HF_ARCHIVE_MARKER\] DONE \| Part (\d+)", text)}
        planned = {int(value) for value in re.findall(r"Part (\d+)(?:/\d+)? \| Ch", text)}
        total = max(planned | yt_parts | hf_parts, default=0)
        return {
            "youtube_progress": {"completed": len(yt_parts), "total": total},
            "hf_progress": {"completed": len(hf_parts), "total": total},
        }

    def reconcile(self, queue):
        runs = self.runs()
        by_task = {}
        for run in runs:
            task_id = task_id_from_run_name(run.get("display_title") or run.get("name"))
            if task_id and task_id not in by_task:
                by_task[task_id] = run
        changed = False
        for task in queue["tasks"]:
            run = by_task.get(task.get("task_id"))
            if not run:
                continue
            run_id = int(run["id"])
            status = run.get("status")
            conclusion = run.get("conclusion")
            if task.get("run_id") != run_id:
                task["run_id"] = run_id
                changed = True
            progress = self.progress_markers(run_id)
            if progress:
                for key, value in progress.items():
                    if task.get(key) != value:
                        task[key] = value
                        changed = True
            if status != "completed" and task.get("status") in {"dispatching", "queued"}:
                task["status"] = "running"
                changed = True
            elif status == "completed" and conclusion == "success" and task.get("status") != "completed":
                task.update({"status": "completed", "reason": None, "retry_at": None, "completed_at": run.get("updated_at")})
                changed = True
            elif status == "completed" and conclusion == "failure" and task.get("status") not in {"stopped", "paused"}:
                reason, retry_at = self.retry_marker(run_id)
                task.update({
                    "status": "waiting_retry" if reason in TRANSIENT_REASONS else "needs_attention",
                    "reason": reason,
                    "retry_at": retry_at.isoformat(),
                })
                changed = True
            elif status == "completed" and conclusion == "cancelled" and task.get("status") == "canceling":
                task.update({"status": "stopped", "reason": "user_cancelled", "retry_at": None})
                changed = True
        return queue, changed

    def dispatch_next(self, queue):
        task = next_task(queue)
        if not task:
            return queue, "No queued task is eligible."
        task_id = task["task_id"]
        task.update({"status": "dispatching", "reason": None, "retry_at": None, "dispatched_at": datetime.now(timezone.utc).isoformat()})
        self.store.save(queue, sha=self.store.load()[1], message=f"Reserve audiobook task {task_id}")
        inputs = {
            "queue_task_id": task_id,
            "catalog_url": task["catalog_url"],
            "start_chap": str(task.get("start_chapter") or 1),
            "end_chap": str(task.get("end_chapter") or 999999),
            "exclude_chapters": ",".join(str(value) for value in task.get("excluded_chapters") or []),
            "zip_password": "",
        }
        try:
            self.request("POST", "/actions/workflows/audiobook.yml/dispatches", json={"ref": "master", "inputs": inputs})
        except Exception as error:
            latest, latest_sha = self.store.load()
            failed = update_task(latest, task_id, status="needs_attention", reason=f"dispatch_failed: {error}")
            self.store.save(failed, sha=latest_sha, message=f"Record dispatch failure for {task_id}")
            raise
        # workflow_dispatch returns 204 without a Run ID. Match the unique
        # task ID embedded in run-name so the GUI can open/cancel the exact Run
        # immediately instead of waiting for the next 15-minute reconciliation.
        run_id = None
        for _ in range(10):
            for run in self.runs():
                if task_id_from_run_name(run.get("display_title") or run.get("name")) == task_id:
                    run_id = int(run["id"])
                    break
            if run_id:
                break
            import time
            time.sleep(3)
        if run_id:
            latest, latest_sha = self.store.load()
            running = update_task(latest, task_id, status="running", run_id=run_id, run_attempt=1)
            self.store.save(running, sha=latest_sha, message=f"Attach Run {run_id} to {task_id}")
        return queue, f"Dispatched {task.get('book_title')} ({task_id}) as Run {run_id or 'pending discovery'}."

    def run(self):
        queue, sha = self.store.load()
        queue, changed = self.reconcile(queue)
        if changed:
            sha = self.store.save(queue, sha=sha, message="Reconcile audiobook queue with GitHub runs")
        active = current_task(queue)
        if active and active.get("status") == "waiting_retry":
            retry_at = parse_time(active.get("retry_at"))
            if retry_at and datetime.now(timezone.utc) >= retry_at:
                run_id = active.get("run_id")
                if run_id:
                    self.request("POST", f"/actions/runs/{run_id}/rerun-failed-jobs")
                    updated = update_task(queue, active["task_id"], status="running", run_attempt=int(active.get("run_attempt") or 1) + 1)
                    self.store.save(updated, sha=sha, message=f"Retry audiobook task {active['task_id']}")
                    return f"Retried {active['task_id']} from its saved checkpoints."
            return f"Current task {active['task_id']} is waiting until {active.get('retry_at')}."
        if active:
            return f"Current task {active['task_id']} is {active['status']}; no second task was started."
        queue, message = self.dispatch_next(queue)
        return message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--branch", default=os.environ.get("QUEUE_STATE_BRANCH", "automation-state"))
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GH_TOKEN are required")
    print(Dispatcher(args.repo, token, args.branch).run())


if __name__ == "__main__":
    main()
