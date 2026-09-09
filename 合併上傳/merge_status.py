"""Collect and format progress for HF full-book merge/upload runs."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def next_scheduler_scan(target, minute=17):
    candidate = target.astimezone(TAIPEI).replace(minute=minute, second=0, microsecond=0)
    if candidate < target.astimezone(TAIPEI):
        candidate += timedelta(hours=1)
    return candidate


def run_book_title(display_title):
    text = str(display_title or "")
    if text.startswith("【HF 合併】"):
        text = text[len("【HF 合併】"):]
    return text.split("｜", 1)[0].strip() or "（無法識別書名）"


def resume_run_book_title(display_title):
    text = str(display_title or "")
    if not text.startswith("【HF 續傳】"):
        return ""
    return text[len("【HF 續傳】"):].strip()


def phase1_text(run, jobs):
    status, conclusion = run.get("status"), run.get("conclusion")
    merge_jobs = [job for job in jobs if str(job.get("name", "")).startswith("merge_and_pause")]
    if status == "queued":
        return "排隊中"
    if status == "completed":
        return "已完成" if conclusion == "success" else f"失敗（{conclusion or '未知'}）"
    completed = sum(job.get("status") == "completed" and job.get("conclusion") == "success" for job in merge_jobs)
    active_steps = []
    for job in merge_jobs:
        for step in job.get("steps") or []:
            if step.get("status") == "in_progress":
                name = str(step.get("name") or "")
                if "Merge pinned" in name:
                    active_steps.append("合併")
                elif "Phase 1" in name:
                    active_steps.append("上傳至 98%")
                else:
                    active_steps.append(name)
    progress = f"{completed}/{len(merge_jobs)} 支完成" if merge_jobs else "準備中"
    return progress + (f"；進行：{', '.join(sorted(set(active_steps)))}" if active_steps else "")


def phase2_summary(states):
    if not states:
        return "尚未產生續傳狀態", "—"
    counts = {}
    for state in states:
        key = state.get("status") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    total = len(states); done = counts.get("complete", 0)
    if done == total:
        phase = f"已完成 {done}/{total} 支"
    elif counts.get("needs_attention"):
        phase = f"需人工處理 {counts['needs_attention']}/{total} 支"
    elif counts.get("resume_dispatched"):
        phase = f"續傳中（已完成 {done}/{total}）"
    else:
        phase = f"等待 24 小時（已完成 {done}/{total}）"
    targets = [parse_time(s.get("target_resume_at")) for s in states if s.get("status") == "paused_at_98"]
    targets = [value for value in targets if value]
    if not targets:
        return phase, "—"
    target = min(targets)
    return phase, f"{target.astimezone(TAIPEI):%m-%d %H:%M} 可續做；排程約 {next_scheduler_scan(target):%m-%d %H:%M}"


def collect_status(repo_id, token, run_gh, limit=50):
    """Return one GUI row per launched book, combining GitHub and HF evidence."""
    from huggingface_hub import HfApi, hf_hub_download

    runs = json.loads(run_gh(
        "run", "list", "--repo", "hub-google/audiobook-generator", "--workflow", "merge-hf-book.yml",
        "--event", "workflow_dispatch", "--limit", str(limit),
        "--json", "databaseId,displayTitle,status,conclusion,createdAt,updatedAt,url",
    ))
    rows = {}
    for run in runs:
        title = run_book_title(run.get("displayTitle"))
        # Only the newest run represents Phase 1 when the same book was launched again.
        if title in rows:
            continue
        jobs = []
        if run.get("status") not in {"queued", "completed"}:
            try:
                jobs = json.loads(run_gh("run", "view", str(run["databaseId"]), "--repo", "hub-google/audiobook-generator", "--json", "jobs")).get("jobs") or []
            except Exception:
                jobs = []
        rows[title] = {"title": title, "phase1_run_id": run["databaseId"], "phase1_run_url": run.get("url", ""),
                       "phase2_run_id": None, "phase2_run_url": "",
                       "phase1": phase1_text(run, jobs), "states": [], "updated_at": run.get("updatedAt", "")}

    resume_runs = json.loads(run_gh(
        "run", "list", "--repo", "hub-google/audiobook-generator", "--workflow", "resume-hf-upload.yml",
        "--event", "workflow_dispatch", "--limit", str(limit),
        "--json", "databaseId,displayTitle,status,conclusion,createdAt,updatedAt,url",
    ))
    for run in resume_runs:
        title = resume_run_book_title(run.get("displayTitle"))
        if not title:
            continue
        row = rows.setdefault(title, {"title": title, "phase1_run_id": None, "phase1_run_url": "",
                                      "phase2_run_id": None, "phase2_run_url": "", "phase1": "已完成",
                                      "states": [], "updated_at": ""})
        if row.get("phase2_run_id") is None:
            row["phase2_run_id"], row["phase2_run_url"] = run["databaseId"], run.get("url", "")
        row["updated_at"] = max(row.get("updated_at") or "", run.get("updatedAt") or "")

    api = HfApi(token=token)
    files = api.list_repo_files(repo_id, repo_type="dataset")
    for path in files:
        if not path.startswith("_system/full_merges/") or not path.endswith("/upload_state.json"):
            continue
        try:
            local = hf_hub_download(repo_id, path, repo_type="dataset", token=token)
            state = json.loads(open(local, encoding="utf-8").read())
            title = str(state.get("book_title") or "").strip()
            if not title and state.get("manifest_path"):
                manifest = hf_hub_download(repo_id, state["manifest_path"], repo_type="dataset", token=token)
                title = str(json.loads(open(manifest, encoding="utf-8").read()).get("book_title") or "").strip()
            if not title:
                continue
            row = rows.setdefault(title, {"title": title, "phase1_run_id": None, "phase1_run_url": "",
                                           "phase2_run_id": None, "phase2_run_url": "", "phase1": "已完成",
                                           "states": [], "updated_at": ""})
            row["states"].append(state)
            if row["phase1"].startswith("失敗") and state.get("status") in {"paused_at_98", "resume_dispatched", "complete"}:
                row["phase1"] = "已完成"
        except Exception:
            continue
    result = []
    for row in rows.values():
        row["phase2"], row["resume"] = phase2_summary(row.pop("states"))
        result.append(row)
    return sorted(result, key=lambda row: row.get("updated_at") or "", reverse=True)
