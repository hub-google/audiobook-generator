"""One-shot queue dispatcher used by queue-dispatcher.yml."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from .cloud_queue import BLOCKING_STATES, GitHubQueueStore, QueueConflict, current_task, format_chapter_label, next_task, settle_interrupted_task, task_id_from_run_name, update_task
    from .book_profiles import GitHubBookProfileStore, get_book_profile, profile_snapshot
except ImportError:
    from cloud_queue import BLOCKING_STATES, GitHubQueueStore, QueueConflict, current_task, format_chapter_label, next_task, settle_interrupted_task, task_id_from_run_name, update_task
    from book_profiles import GitHubBookProfileStore, get_book_profile, profile_snapshot


def _find_gh() -> str:
    """Resolve GitHub CLI executable path, checking PATH then common Windows locations."""
    found = shutil.which("gh")
    if found:
        return found
    installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if installed.exists():
        return str(installed)
    raise FileNotFoundError("找不到 GitHub CLI (gh)。請先安裝並執行 gh auth login。")


TRANSIENT_REASONS = {
    "quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "backendError", "otherError",
    "thumbnailRateLimit", "captionUploadFailed", "playlistInsertFailed", "publishFailed",
    "retryable YouTube API failure",
}


def failed_artifact_source_candidates(queue, profile_id, current_task_id="", current_task=None):
    """Return reusable failed/cancelled Run IDs for one stable book profile.

    A cancelled Run may still contain valid per-worker checkpoints uploaded by
    ``if: always()`` cleanup steps.  The dispatcher performs live Run and
    artifact checks before locking any recorded candidate.
    """
    candidates = []
    sequence = 0
    tasks = list(queue.get("queue") or []) + list(queue.get("completed") or [])
    if current_task is not None:
        tasks = [task for task in tasks if task.get("task_id") != current_task_id] + [current_task]
    for task in tasks:
        if str(task.get("book_profile_id") or "") != str(profile_id or ""):
            continue
        history = list(task.get("run_history") or [])
        if (task.get("task_id") != current_task_id and task.get("run_id")
                and task.get("run_conclusion") in {"failure", "cancelled"}):
            history.append({
                "run_id": task.get("run_id"),
                "conclusion": task.get("run_conclusion"),
                "ended_at": task.get("run_completed_at") or task.get("updated_at") or "",
            })
        for item in history:
            if item.get("conclusion") not in {"failure", "cancelled"}:
                continue
            run_id = item.get("run_id")
            if not str(run_id or "").isdigit():
                continue
            sequence += 1
            candidates.append((str(item.get("ended_at") or ""), sequence, int(run_id)))
    newest_by_run = {}
    for ended_at, order, run_id in candidates:
        newest_by_run[run_id] = max(newest_by_run.get(run_id, ("", -1)), (ended_at, order))
    return [
        run_id for run_id, _ in sorted(
            newest_by_run.items(), key=lambda item: (item[1][0], item[1][1]), reverse=True,
        )
    ]


def artifact_source_run_id(queue, profile_id, current_task_id="", current_task=None):
    """Return the newest recorded failed Run; live validation is done by Dispatcher."""
    candidates = failed_artifact_source_candidates(
        queue, profile_id, current_task_id, current_task=current_task,
    )
    return candidates[0] if candidates else None

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
    def __init__(self, repo, token, branch="automation-state", force=False):
        self.repo = repo
        self.token = token
        self.store = GitHubQueueStore(repo, token, branch=branch)
        self.profile_store = GitHubBookProfileStore(repo, token, branch=branch)
        self.force = bool(force)

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

    def run_artifact_names(self, run_id):
        """Return non-expired artifact names for one existing Actions Run."""
        response = self.request(
            "GET", f"/actions/runs/{run_id}/artifacts", params={"per_page": 100},
        )
        return {
            str(item.get("name") or "")
            for item in response.json().get("artifacts", [])
            if not item.get("expired") and item.get("name")
        }

    def select_artifact_source_run_id(self, queue, profile_id, current_task_id="", current_task=None):
        """Lock the newest completed failed/cancelled Run with worker artifacts."""
        for run_id in failed_artifact_source_candidates(
            queue, profile_id, current_task_id, current_task=current_task,
        ):
            run = self.run_by_id(run_id)
            if not run:
                continue
            if (run.get("status") != "completed"
                    or run.get("conclusion") not in {"failure", "cancelled"}):
                continue
            names = self.run_artifact_names(run_id)
            if "shared-config" not in names:
                continue
            if not any(name.startswith("video-worker-") for name in names):
                continue
            return int(run_id)
        return None

    def run_uses_current_master(self, run_id):
        """Only rerun in place when GitHub would execute the current code."""
        run = self.run_by_id(run_id)
        if not run or not run.get("head_sha"):
            return False
        master = self.request("GET", "/git/ref/heads/master").json()
        master_sha = ((master.get("object") or {}).get("sha") or "").strip()
        return bool(master_sha and run["head_sha"] == master_sha)

    def run_jobs(self, run_id):
        try:
            response = requests.get(
                f"{self.api}/actions/runs/{run_id}/jobs",
                headers=self.headers, params={"per_page": 100}, timeout=30,
            )
            if response.status_code == 200:
                return response.json().get("jobs", [])
        except Exception:
            pass
        return []

    def job_log(self, job_id):
        try:
            response = requests.get(
                f"{self.api}/actions/jobs/{job_id}/logs",
                headers=self.headers, timeout=15, allow_redirects=True,
            )
            if response.status_code == 200 and response.text:
                return response.text
        except Exception:
            pass
        try:
            res = subprocess.run(
                [_find_gh(), "run", "view", f"--job={job_id}", "--repo", self.repo, "--log"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        return ""

    def retry_marker(self, run_id):
        text = ""
        try:
            command = [_find_gh(), "run", "view", str(run_id), "--repo", self.repo, "--log-failed"]
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20, check=False,
            )
            text = (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.SubprocessError):
            pass

        if not text or "too many API requests" in text:
            jobs = self.run_jobs(run_id)
            failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]
            logs = [self.job_log(j["id"]) for j in failed_jobs if j.get("id")]
            text = "\n".join(log for log in logs if log)

        if not text:
            return "otherError", datetime.now(timezone.utc) + timedelta(hours=2)

        reason_match = re.findall(r"reason=([^ |\r\n]+)", text)
        retry_match = re.findall(r"retry(?: after|_at=)([0-9T:.+\-Z]+)", text)
        reason = reason_match[-1] if reason_match else "otherError"
        if "429 Too Many Requests" in text or "rate limit" in text.lower():
            reason = "rateLimitExceeded"
        retry_at = parse_time(retry_match[-1]) if retry_match else None
        return reason, retry_at or (datetime.now(timezone.utc) + timedelta(hours=2))

    def progress_markers(self, run_id):
        text = ""
        try:
            result = subprocess.run(
                [_find_gh(), "run", "view", str(run_id), "--repo", self.repo, "--log"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False,
            )
            if result.returncode == 0 and result.stdout and result.stdout.strip():
                text = result.stdout
        except (OSError, subprocess.SubprocessError):
            pass

        if not text:
            jobs = self.run_jobs(run_id)
            target_keywords = ["publication", "publish", "part plan"]
            priority_jobs = [
                j for j in jobs
                if any(kw in (j.get("name") or "").lower() for kw in target_keywords)
            ]
            logs = [self.job_log(j["id"]) for j in priority_jobs if j.get("id")]
            text = "\n".join(log for log in logs if log)

        if not text:
            return None

        yt_parts = {int(value) for value in re.findall(r"\[API_UPLOAD_MARKER\] DONE \| Part (\d+)", text)}
        hf_parts = {int(value) for value in re.findall(r"\[HF_(?:MEDIA|ARCHIVE)_MARKER\] DONE \| Part (\d+)", text)}
        planned = {int(value) for value in re.findall(r"Part (\d+)(?:/\d+)? \| Ch", text)}
        total = max(planned | yt_parts | hf_parts, default=0)
        if total == 0 and not yt_parts and not hf_parts:
            return None
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
        for task in list(queue["queue"]):
            # Recover the GUI/dispatcher race where GUI records the cancelled
            # Run first.  The durable edit intent must still win.
            if task.get("status") == "interrupted" and task.get("requeue_after_edit"):
                queue = settle_interrupted_task(queue, task["task_id"])
                changed = True
                continue
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
                        queue = settle_interrupted_task(
                            queue, task["task_id"], reason="run_not_found",
                            conclusion="missing", ended_at=datetime.now(timezone.utc).isoformat(),
                        )
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
                )
                changed = True
            elif status == "completed" and conclusion == "failure" and task.get("status") not in {"stopped", "paused"}:
                if task.get("status") == "waiting_retry" and task.get("retry_at"):
                    continue
                existing_retry_at = parse_time(task.get("retry_at"))
                reason, retry_at = self.retry_marker(run_id)
                if reason not in TRANSIENT_REASONS:
                    task.update({
                        "status": "needs_attention",
                        "reason": reason or "permanentFailure",
                        "retry_at": None,
                    })
                    changed = True
                    continue
                retry_at = existing_retry_at or retry_at or (datetime.now(timezone.utc) + timedelta(hours=2))
                task.update({
                    "status": "waiting_retry",
                    "reason": reason,
                    "retry_at": retry_at.isoformat(),
                })
                changed = True
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
                progress = self.progress_markers(task["run_id"])
                if progress:
                    for key, value in progress.items():
                        if value.get("total", 0) > 0 and task.get(key) != value:
                            task[key] = value
                            changed = True
        return queue, changed

    def dispatch_next(self, queue):
        active_runs = [r for r in self.runs() if r.get("status") != "completed"]
        if active_runs:
            first_run = active_runs[0]
            return queue, f"Audiobook run {first_run.get('id')} ({first_run.get('display_title') or first_run.get('name')}) is already active on GitHub. Will not dispatch another task."

        task = next_task(queue)
        if not task:
            return queue, "No queued task is eligible."
        task_id = task["task_id"]
        profiles, _ = self.profile_store.load()
        profile_id, profile = get_book_profile(
            profiles, task.get("catalog_url") or "", task.get("book_title") or "",
        )
        snapshot = profile_snapshot(profile_id, profile)
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
        source_run_id = self.select_artifact_source_run_id(
            queue, profile_id, task_id, current_task=task,
        )
        task["artifact_source_run_id"] = source_run_id
        task.update({"status": "dispatching", "reason": None, "retry_at": None, "dispatched_at": datetime.now(timezone.utc).isoformat()})
        queue = update_task(
            queue, task_id,
            book_profile_id=profile_id,
            profile_snapshot=snapshot,
            artifact_source_run_id=source_run_id,
            status="dispatching",
            reason=None,
            retry_at=None,
            dispatched_at=task["dispatched_at"],
        )
        self.store.save(queue, sha=self.store.load()[1], message=f"Reserve audiobook task {task_id}")

        start_int = int(task.get("start_chapter") or 1)
        end_int = int(task.get("end_chapter") or 999999)
        excluded = task.get("excluded_chapters") or []
        renumber = bool(task.get("renumber_selected"))
        chapter_label = format_chapter_label(start_int, end_int, excluded_chapters=excluded, renumber_selected=renumber)

        inputs = {
            "book_title": task.get("book_title") or "待解析書名",
            "chapter_label": chapter_label,
            "queue_task_id": task_id,
            "resume_source_run_id": str(source_run_id or ""),
            "catalog_url": task["catalog_url"],
            "start_chap": str(start_int),
            "end_chap": str(end_int),
            "exclude_chapters": ",".join(str(value) for value in sorted(task.get("excluded_chapters") or [])),
            "renumber_selected": "true" if renumber else "false",
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
        dispatch_requested_at = datetime.now(timezone.utc)
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
                created_at = parse_time(run.get("created_at"))
                if (
                    task_id_from_run_name(run.get("display_title") or run.get("name")) == task_id
                    and created_at is not None
                    and created_at >= dispatch_requested_at
                ):
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
        else:
            # The durable record remains reserved as dispatching, but never
            # claim that a Run started until GitHub returns observable evidence.
            queue = update_task(queue, task_id, status="queued")
        return queue, f"Dispatched {task.get('book_title')} ({task_id}) as Run {run_id or 'pending discovery'}."

    def summary(self, queue, action, task=None):
        """Render a useful operator-facing Markdown report for Actions summary."""
        now = datetime.now(timezone.utc)
        queued = [item for item in queue.get("queue", []) if item.get("status") == "queued"]
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
        # GUI polling and workflow_run commonly reconcile the same completion
        # concurrently. Reload and recompute after an optimistic-lock conflict.
        for attempt in range(5):
            queue, sha = self.store.load()
            queue, changed = self.reconcile(queue)
            if not changed:
                break
            try:
                sha = self.store.save(queue, sha=sha, message="Reconcile audiobook queue with GitHub runs")
                break
            except QueueConflict:
                if attempt == 4:
                    raise
        active = current_task(queue)
        if active and active.get("status") in {"waiting_retry", "interrupted"}:
            retry_at = parse_time(active.get("retry_at"))
            is_ready = self.force or not retry_at or (datetime.now(timezone.utc) >= retry_at)
            if is_ready:
                run_id = active.get("run_id")
                if run_id and not self.force and self.run_uses_current_master(run_id):
                    try:
                        self.request("POST", f"/actions/runs/{run_id}/rerun-failed-jobs")
                        updated = update_task(queue, active["task_id"], status="running", run_attempt=int(active.get("run_attempt") or 1) + 1, retry_at=None, reason=None)
                        self.store.save(updated, sha=sha, message=f"Retry audiobook task {active['task_id']}")
                        return self.summary(updated, "retried", current_task(updated))
                    except Exception as rerun_err:
                        logging.warning("Rerun failed; will dispatch fresh run: %s", rerun_err)
                elif run_id and not self.force:
                    logging.info("Run %s does not use current master; dispatching a fresh run.", run_id)
                # If rerun failed or self.force, unblock to queued for immediate fresh dispatch
                queue = update_task(queue, active["task_id"], status="queued", retry_at=None, reason=None)
                sha = self.store.save(queue, sha=sha, message=f"Unblock task {active['task_id']} for immediate dispatch")
                active = None

        if active:
            return self.summary(queue, "active", active)
        queue, _message = self.dispatch_next(queue)
        dispatched = current_task(queue)
        # A 204 dispatch response is not proof that GitHub created a run. Never
        # label the still-queued prospective task as launched unless a run was
        # observed and attached by dispatch_next().
        return self.summary(queue, "dispatched", dispatched) if dispatched else self.summary(queue, "idle")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--branch", default=os.environ.get("QUEUE_STATE_BRANCH", "automation-state"))
    parser.add_argument("--force", action="store_true", help="Force immediate dispatch ignoring retry_at")
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GH_TOKEN are required")
    print(Dispatcher(args.repo, token, args.branch, force=args.force).run())


if __name__ == "__main__":
    main()
