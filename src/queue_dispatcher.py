"""One-shot queue dispatcher used by queue-dispatcher.yml."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from .cloud_queue import BLOCKING_STATES, MAX_NATIVE_PREFLIGHT_RERUNS, preflight_status, GitHubQueueStore, QueueConflict, current_task, format_chapter_label, next_task, settle_interrupted_task, task_id_from_run_name, update_task
    from .book_profiles import GitHubBookProfileStore, get_book_profile, profile_snapshot
except ImportError:
    from cloud_queue import BLOCKING_STATES, MAX_NATIVE_PREFLIGHT_RERUNS, preflight_status, GitHubQueueStore, QueueConflict, current_task, format_chapter_label, next_task, settle_interrupted_task, task_id_from_run_name, update_task
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


from .dispatch_retry_policy import TRANSIENT_REASONS


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
    "preparing_assets": "TXT／封面前置處理中",
    "waiting_review": "等待人工審核",
    "processing": "後製／上傳中",
    "running": "製作中",
    "waiting_retry": "等待自動重試",
    "needs_attention": "需要人工處理",
    "canceling": "正在取消",
}

# A scrape rerun gets a fresh GitHub-hosted runner.  This is intentionally
# separate from in-process HTTP retries, which keep the same blocked egress IP.
MAX_IMMEDIATE_SCRAPE_RERUNS = MAX_NATIVE_PREFLIGHT_RERUNS
MAX_IMMEDIATE_COVER_RERUNS = MAX_NATIVE_PREFLIGHT_RERUNS
_AUTO_TASK = object()


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


from .dispatch_github_gateway import GitHubRunGateway


class Dispatcher(GitHubRunGateway):
    def __init__(self, repo, token, branch="automation-state", force=False,
                 trigger_run_id=None, trigger_workflow_name="",
                 trigger_conclusion="", trigger_completed_at="", trigger_attempt=None):
        self.repo = repo
        self.token = token
        self.store = GitHubQueueStore(repo, token, branch=branch)
        self.profile_store = GitHubBookProfileStore(repo, token, branch=branch)
        self.force = bool(force)
        self.trigger_run_id = int(trigger_run_id) if str(trigger_run_id or "").isdigit() else None
        self.trigger_workflow_name = str(trigger_workflow_name or "")
        self.trigger_conclusion = str(trigger_conclusion or "")
        self.trigger_completed_at = str(trigger_completed_at or "")
        self.trigger_attempt = int(trigger_attempt or 0)
        # workflow_dispatch returns no Run ID. Poll briefly until GitHub indexes
        # every requested Run; GUI reconciliation remains the durable fallback.
        self.dispatch_discovery_delays = (0, 1, 2, 3, 4)

        self.headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.api = f"https://api.github.com/repos/{repo}"

    def triggered_run(self, workflow_name):
        """Return the authoritative completed workflow_run event, when applicable.

        GitHub may briefly return an older ``in_progress`` value from the Runs
        API after emitting ``workflow_run: completed``.  The event is the
        authoritative edge that releases the queue; synthetic data is used only
        for the exact bound Run ID and never for an unrelated active Run.
        """
        if (not self.trigger_run_id or not self.trigger_conclusion
                or self.trigger_workflow_name != workflow_name):
            return None
        return {
            "id": self.trigger_run_id,
            "status": "completed",
            "conclusion": self.trigger_conclusion,
            **({"run_attempt": self.trigger_attempt} if self.trigger_attempt else {}),
            "updated_at": self.trigger_completed_at or datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def apply_completion_event(run, trigger, bound_attempt=0):
        if not trigger:
            return run
        current_attempt = int((run or {}).get("run_attempt") or bound_attempt or 1)
        event_attempt = int(trigger.get("run_attempt") or current_attempt)
        if event_attempt < current_attempt:
            return run
        return {**(run or {}), **trigger, "run_attempt": event_attempt}

    def is_authoritatively_completed_run(self, run):
        return bool(
            self.trigger_run_id
            and self.trigger_conclusion
            and int(run.get("id") or 0) == self.trigger_run_id
        )

    def recover_preflight_controller(self, task):
        """Recover an abandoned controller without competing with a live one."""
        if task.get("workflow_phase") != "preflight" or task.get("status") != "preparing_assets":
            return False
        for stage_name in ("scrape", "cover"):
            stage = (task.get("stages") or {}).get(stage_name) or {}
            if stage.get("status") != "failed" or stage.get("reason") != "failure":
                continue
            run_id = stage.get("run_id")
            if not run_id or int(stage.get("run_attempt") or 1) > MAX_NATIVE_PREFLIGHT_RERUNS:
                continue
            source = self.run_by_id(run_id)
            if not source or source.get("status") != "completed" or source.get("conclusion") != "failure":
                continue
            stamp = parse_time(source.get("updated_at"))
            if not stamp or datetime.now(timezone.utc) - stamp < timedelta(minutes=2):
                continue
            if int(source.get("run_attempt") or 1) > MAX_NATIVE_PREFLIGHT_RERUNS:
                continue
            workflow = f"{stage_name}-rerun.yml"
            response = self.request("GET", f"/actions/workflows/{workflow}/runs", params={"per_page": 100})
            controllers = [r for r in response.json().get("workflow_runs", [])
                           if str(r.get("display_title") or r.get("name") or "").endswith(f"#{run_id}")]
            if any(r.get("status") != "completed" for r in controllers):
                continue
            # A just-finished controller can be observed before the source
            # attempt is indexed. Allow a short settling period.
            recent = [parse_time(r.get("updated_at")) for r in controllers]
            if any(t and datetime.now(timezone.utc) - t < timedelta(minutes=2) for t in recent):
                continue
            try:
                if stage_name == "scrape":
                    from .scrape_rerun_controller import decide_rerun
                else:
                    from .cover_rerun_controller import decide_rerun
            except ImportError:
                if stage_name == "scrape":
                    from scrape_rerun_controller import decide_rerun
                else:
                    from cover_rerun_controller import decide_rerun
            decision = decide_rerun(source, self.run_jobs(run_id))
            if not decision.should_rerun:
                continue
            # The workflow itself serializes all controllers for this source.
            self.request("POST", f"/actions/workflows/{workflow}/dispatches",
                         json={"ref": "master", "inputs": {"source_run_id": str(run_id)}})
            logging.info("Recovered %s native rerun controller for source %s", stage_name, run_id)
            return True
        return False

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
        """Reconcile durable state through the single state-machine module."""
        from .dispatch_reconciliation import reconcile
        return reconcile(self, queue)

    def dispatch_next(self, queue, reservation_sha=_AUTO_TASK):
        """Reserve and dispatch the next task using the shared execution service."""
        from .dispatch_execution import dispatch_next
        return dispatch_next(self, queue, reservation_sha)

    def summary(self, queue, action, task=_AUTO_TASK):
        """Render a useful operator-facing Markdown report for Actions summary."""
        now = datetime.now(timezone.utc)
        queued = [item for item in queue.get("queue", []) if item.get("status") == "queued"]
        active = current_task(queue) if task is _AUTO_TASK else task

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
                    # Do not convert an ambiguous request or a post-dispatch
                    # storage conflict into a new production Run. Reconcile
                    # the existing Run first on the next dispatcher attempt.
                    self.request("POST", f"/actions/runs/{run_id}/rerun-failed-jobs")
                    updated = update_task(queue, active["task_id"], status="running", run_attempt=int(active.get("run_attempt") or 1) + 1, retry_at=None, reason=None)
                    self.store.save(updated, sha=sha, message=f"Retry audiobook task {active['task_id']}")
                    return self.summary(updated, "retried", current_task(updated))
                elif run_id and not self.force:
                    logging.info("Run %s does not use current master; dispatching a fresh run.", run_id)
                # For an explicitly forced or stale-code Run, dispatch a fresh Run.
                queue = update_task(queue, active["task_id"], status="queued", retry_at=None, reason=None)
                sha = self.store.save(queue, sha=sha, message=f"Unblock task {active['task_id']} for immediate dispatch")
                active = None

        if active:
            if self.recover_preflight_controller(active):
                return self.summary(queue, "active", active) + "\n\n- 已重新啟動中斷的前置作業重跑控制器。"
            return self.summary(queue, "active", active)
        queue, _message = self.dispatch_next(queue, reservation_sha=sha)
        dispatched = current_task(queue)
        # A 204 dispatch response is not proof that GitHub created a run. Never
        # label the still-queued prospective task as launched unless a run was
        # observed and attached by dispatch_next().
        if dispatched and not any(
                dispatched.get(key) for key in
                ("run_id", "scrape_run_id", "cover_run_id", "processing_run_id")):
            report = self.summary(queue, "idle", None)
            return report + "\n\n- **Run 建立狀態：** 已保留啟動鎖，等待 GitHub 建立可追蹤的 Run。"
        return self.summary(queue, "dispatched", dispatched) if dispatched else self.summary(queue, "idle")


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--branch", default=os.environ.get("QUEUE_STATE_BRANCH", "automation-state"))
    parser.add_argument("--force", action="store_true", help="Force immediate dispatch ignoring retry_at")
    parser.add_argument("--trigger-run-id", default=os.environ.get("TRIGGER_RUN_ID"))
    parser.add_argument("--trigger-workflow-name", default=os.environ.get("TRIGGER_WORKFLOW_NAME", ""))
    parser.add_argument("--trigger-conclusion", default=os.environ.get("TRIGGER_CONCLUSION", ""))
    parser.add_argument("--trigger-completed-at", default=os.environ.get("TRIGGER_COMPLETED_AT", ""))
    parser.add_argument("--trigger-attempt", default=os.environ.get("TRIGGER_ATTEMPT", ""))
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        raise SystemExit("GITHUB_REPOSITORY and GH_TOKEN are required")
    print(Dispatcher(
        args.repo, token, args.branch, force=args.force,
        trigger_run_id=args.trigger_run_id,
        trigger_workflow_name=args.trigger_workflow_name,
        trigger_conclusion=args.trigger_conclusion,
        trigger_completed_at=args.trigger_completed_at,
        trigger_attempt=args.trigger_attempt,
    ).run())


if __name__ == "__main__":
    main()
