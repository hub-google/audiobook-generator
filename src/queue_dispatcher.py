"""One-shot queue dispatcher used by queue-dispatcher.yml."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from .cloud_queue import BLOCKING_STATES, GitHubQueueStore, current_task, mark_task_interrupted, next_task, requeue_task_after_active, task_id_from_run_name, update_task
except ImportError:
    from cloud_queue import BLOCKING_STATES, GitHubQueueStore, current_task, mark_task_interrupted, next_task, requeue_task_after_active, task_id_from_run_name, update_task


TRANSIENT_REASONS = {
    "quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "backendError", "otherError",
    "thumbnailRateLimit", "captionUploadFailed", "playlistInsertFailed", "publishFailed",
    "retryable YouTube API failure",
}

TAIPEI = ZoneInfo("Asia/Taipei")
STATUS_LABELS = {
    "dispatching": "正在啟動",
    "running": "製作中",
    "waiting_retry": "等待自動重試",
    "needs_attention": "需要人工處理",
    "canceling": "正在取消",
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

    def run_by_id(self, run_id):
        response = requests.get(f"{self.api}/actions/runs/{run_id}", headers=self.headers, timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API GET /actions/runs/{run_id} failed ({response.status_code}): {response.text}")
        return response.json()

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
        hf_parts = {int(value) for value in re.findall(r"\[HF_(?:MEDIA|ARCHIVE)_MARKER\] DONE \| Part (\d+)", text)}
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
            if task_id:
                by_task.setdefault(task_id, []).append(run)
        changed = False
        for task in queue["tasks"]:
            candidates = by_task.get(task.get("task_id"), [])
            retry_requested_at = parse_time(task.get("retry_requested_at"))
            if retry_requested_at:
                candidates = [run for run in candidates if (parse_time(run.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= retry_requested_at]
            run = candidates[0] if candidates else None
            if not run:
                bound_run_id = task.get("run_id")
                if bound_run_id and task.get("status") in BLOCKING_STATES:
                    run = self.run_by_id(bound_run_id)
                    if run is None:
                        restart = bool(task.get("requeue_after_edit"))
                        queue = mark_task_interrupted(
                            queue, task["task_id"], reason="run_not_found",
                            conclusion="missing", ended_at=datetime.now(timezone.utc).isoformat(),
                        )
                        if restart:
                            queue = requeue_task_after_active(queue, task["task_id"])
                            queue = update_task(queue, task["task_id"], requeue_after_edit=False)
                        changed = True
                        continue
                if not run:
                    continue
            run_id = int(run["id"])
            status = run.get("status")
            conclusion = run.get("conclusion")
            if task.get("run_id") != run_id:
                task["run_id"] = run_id
                changed = True
            if task.get("requeue_after_edit") and status != "completed":
                cancel_response = requests.post(
                    f"{self.api}/actions/runs/{run_id}/cancel", headers=self.headers, timeout=30,
                )
                if cancel_response.status_code not in (200, 202, 409):
                    raise RuntimeError(
                        f"GitHub API POST /actions/runs/{run_id}/cancel failed "
                        f"({cancel_response.status_code}): {cancel_response.text}"
                    )
                if task.get("status") != "canceling":
                    task["status"] = "canceling"
                    changed = True
                continue
            progress = None if status == "completed" and conclusion == "cancelled" else self.progress_markers(run_id)
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
            elif status == "completed" and conclusion == "cancelled" and task.get("status") not in {"interrupted", "paused"}:
                restart = bool(task.get("requeue_after_edit"))
                queue = mark_task_interrupted(
                    queue, task["task_id"], reason="run_cancelled",
                    conclusion="cancelled", ended_at=run.get("updated_at"),
                )
                if restart:
                    queue = requeue_task_after_active(queue, task["task_id"])
                    queue = update_task(queue, task["task_id"], requeue_after_edit=False)
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
            "book_title": task.get("book_title") or "待解析書名",
            "queue_task_id": task_id,
            "catalog_url": task["catalog_url"],
            "start_chap": str(task.get("start_chapter") or 1),
            "end_chap": str(task.get("end_chapter") or 999999),
            "exclude_chapters": ",".join(str(value) for value in task.get("excluded_chapters") or []),
            "renumber_selected": "true" if task.get("renumber_selected") else "false",
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
            task.update({"status": "running", "run_id": run_id, "run_attempt": 1})
            latest, latest_sha = self.store.load()
            running = update_task(latest, task_id, status="running", run_id=run_id, run_attempt=1)
            self.store.save(running, sha=latest_sha, message=f"Attach Run {run_id} to {task_id}")
        return queue, f"Dispatched {task.get('book_title')} ({task_id}) as Run {run_id or 'pending discovery'}."

    def summary(self, queue, action, task=None):
        """Render a useful operator-facing Markdown report for Actions summary."""
        now = datetime.now(timezone.utc)
        queued = [item for item in queue.get("tasks", []) if item.get("status") == "queued"]
        active = task or current_task(queue)

        if action == "dispatched":
            headline = f"🚀 **已啟動《{active.get('book_title') or '待解析書名'}》**"
            action_text = "已保留佇列中的下一項任務，並啟動有聲書製作流程。"
        elif action == "retried":
            headline = f"🔄 **已自動重試《{active.get('book_title') or '待解析書名'}》**"
            action_text = "重跑失敗的工作，並沿用已保存的章節及上傳檢查點。"
        elif active and active.get("status") == "waiting_retry":
            headline = f"🕒 **《{active.get('book_title') or '待解析書名'}》正在等待自動重試**"
            action_text = "尚未到達重試時間，本次沒有發出重試請求，也沒有啟動下一本。"
        elif active and active.get("status") == "needs_attention":
            headline = f"🔴 **《{active.get('book_title') or '待解析書名'}》需要人工處理**"
            action_text = "目前錯誤無法自動恢復；此任務會阻擋後續佇列。"
        elif active:
            headline = f"🟡 **《{active.get('book_title') or '待解析書名'}》仍在製作**"
            action_text = "已同步目前進度；為避免同時製作兩本小說，本次未啟動下一本。"
        else:
            headline = "⚪ **目前沒有待製作小說**"
            action_text = "已完成佇列檢查，沒有可啟動的任務。"

        lines = [
            "## 本次調度結果", "", headline, "",
            f"- **本次動作：** {action_text}",
            f"- **檢查時間：** {now.astimezone(TAIPEI):%Y-%m-%d %H:%M:%S}（台北時間）",
            "- **下次定期檢查：** 最多約 15 分鐘後",
        ]

        if active:
            status = STATUS_LABELS.get(active.get("status"), active.get("status") or "未知")
            run_id = active.get("run_id")
            lines += ["", "## 目前任務", "", f"### 《{active.get('book_title') or '待解析書名'}》", "",
                      f"- **任務編號：** `{active.get('task_id')}`", f"- **狀態：** {status}"]
            if run_id:
                lines.append(f"- **製作流程：** [GitHub Actions Run {run_id}](https://github.com/{self.repo}/actions/runs/{run_id})")
            started = parse_time(active.get("dispatched_at"))
            if started:
                elapsed = max(0, int((now - started).total_seconds()))
                hours, remainder = divmod(elapsed, 3600)
                minutes = remainder // 60
                lines.append(f"- **開始時間：** {started.astimezone(TAIPEI):%Y-%m-%d %H:%M:%S}（已執行 {hours} 小時 {minutes} 分）")
            lines.append(f"- **執行次數：** 第 {int(active.get('run_attempt') or 1)} 次")
            reason = active.get("reason")
            if reason:
                lines.append(f"- **原因：** `{reason}`")
            retry_at = parse_time(active.get("retry_at"))
            if retry_at:
                lines.append(f"- **預計重試：** {retry_at.astimezone(TAIPEI):%Y-%m-%d %H:%M:%S}（台北時間）")

            yt = active.get("youtube_progress") or {}
            hf = active.get("hf_progress") or {}
            lines += ["", "### 發布進度", "", "| 項目 | 已完成 | 總數 |", "|---|---:|---:|",
                      f"| YouTube | {int(yt.get('completed') or 0)} Part | {int(yt.get('total') or 0)} Part |",
                      f"| Hugging Face 備份 | {int(hf.get('completed') or 0)} Part | {int(hf.get('total') or 0)} Part |"]

        next_up = queued[0] if queued else None
        lines += ["", "## 等待中的佇列", "", f"- **等待任務：** {len(queued)} 本"]
        if next_up:
            end = next_up.get("end_chapter") or "最後一章"
            lines += [f"- **下一本：** 《{next_up.get('book_title') or '待解析書名'}》",
                      f"- **章節範圍：** 第 {next_up.get('start_chapter') or 1}–{end} 章",
                      "- **預計啟動：** 目前任務完成或解除阻塞後"]
        else:
            lines.append("- **下一本：** 無")

        lines += ["", "## 這次實際執行的動作", "", "- 已讀取雲端佇列",
                  "- 已比對有聲書製作流程的 GitHub Actions 狀態", "- 已同步可取得的 YouTube 與 Hugging Face 進度"]
        if action == "dispatched":
            lines.append("- 已啟動佇列中的下一本小說")
        elif action == "retried":
            lines.append("- 已重新執行失敗的工作")
        elif active:
            lines.append("- 未啟動第二本小說")
        else:
            lines.append("- 佇列中沒有可啟動的任務")
        return "\n".join(lines)

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
                    return self.summary(updated, "retried", current_task(updated))
            return self.summary(queue, "waiting", active)
        if active:
            return self.summary(queue, "active", active)
        prospective = next_task(queue)
        queue, _message = self.dispatch_next(queue)
        dispatched = current_task(queue)
        if not dispatched and prospective:
            dispatched = next((item for item in queue.get("tasks", []) if item.get("task_id") == prospective.get("task_id")), prospective)
        return self.summary(queue, "dispatched", dispatched) if dispatched else self.summary(queue, "idle")


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
