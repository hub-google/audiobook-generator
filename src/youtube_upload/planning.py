"""Artifact scanning, chapter inventory validation, and Part planning."""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .metadata import _chapter_title
from .playlists import parse_chapter_info

try:
    from ..part_builder import (
        duration_from_srt,
        get_media_duration,
        parse_chapter_num,
    )
except ImportError:
    from part_builder import (
        duration_from_srt,
        get_media_duration,
        parse_chapter_num,
    )


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def _find_gh() -> str:
    """Resolve GitHub CLI executable path, checking PATH then common Windows locations."""
    import shutil as _shutil
    found = _shutil.which("gh")
    if found:
        return found
    installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if installed.exists():
        return str(installed)
    raise FileNotFoundError("找不到 GitHub CLI (gh)。請先安裝並執行 gh auth login。")


def get_run_artifact_names(run_id, repo):
    subproc_run = _get_symbol("subprocess", subprocess).run
    cmd = [
        _find_gh(), "api", "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        "--jq", ".artifacts[].name",
    ]
    res = subproc_run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"Failed to fetch artifacts for run {run_id}: {res.stderr}")
        return []
    all_names = [n.strip() for n in res.stdout.splitlines() if n.strip()]
    return select_worker_artifacts(all_names)


def select_worker_artifacts(all_names):
    """Select one artifact per worker, preferring the lightweight MP4 artifact."""
    selected = {}
    for prefix in ("video-worker-", "mp4-worker-"):
        for name in all_names:
            if name.startswith(prefix):
                selected[artifact_worker_index(name)] = name
    return [selected[index] for index in sorted(selected)]


def artifact_worker_index(name):
    """Extract the worker id, never the ``4`` embedded in the ``mp4`` prefix."""
    match = re.search(r"(?:mp4|video|manifest)-worker-(\d+)$", name)
    if not match:
        raise ValueError(f"無法識別 Worker Artifact 名稱：{name}")
    return int(match.group(1))


def get_run_manifest_artifact_names(run_id, repo):
    subproc_run = _get_symbol("subprocess", subprocess).run
    cmd = [
        _find_gh(), "api", "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        "--jq", ".artifacts[].name",
    ]
    res = subproc_run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"Failed to fetch manifest artifacts for run {run_id}: {res.stderr}")
        return []
    all_names = [n.strip() for n in res.stdout.splitlines() if n.strip()]
    return select_manifest_artifacts(all_names)


def select_manifest_artifacts(all_names):
    """Select one manifest artifact per worker."""
    selected = {}
    for name in all_names:
        if name.startswith("manifest-worker-"):
            try:
                selected[artifact_worker_index(name)] = name
            except ValueError:
                pass
    return [selected[index] for index in sorted(selected)]


def scan_artifact_chapters(artifact_dir, artifact_name):
    """Inventory chapter media in one downloaded artifact."""
    media_dur_fn = _get_symbol("get_media_duration", get_media_duration)
    parse_num_fn = _get_symbol("parse_chapter_num", parse_chapter_num)

    # 1. Manifest-First: check if a worker manifest JSON exists in the artifact directory
    manifest_candidates = glob.glob(os.path.join(artifact_dir, "**", "manifest-worker-*.json"), recursive=True)
    if not manifest_candidates:
        manifest_candidates = glob.glob(os.path.join(artifact_dir, "**", "manifest-*.json"), recursive=True)
    if not manifest_candidates:
        manifest_candidates = [
            f for f in glob.glob(os.path.join(artifact_dir, "**", "*.json"), recursive=True)
            if not os.path.basename(f).startswith("state") and not os.path.basename(f).startswith("parts-plan")
        ]

    for mf in manifest_candidates:
        try:
            with open(mf, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("chapters"), list) and data["chapters"]:
                chapters = []
                for entry in data["chapters"]:
                    chap_num = int(entry["chap_num"])
                    dur = float(entry.get("dur") or 0)

                    # Resolve video file path
                    video_rel = entry.get("video_relpath")
                    video_path = None
                    if video_rel:
                        cand = os.path.join(artifact_dir, video_rel)
                        if os.path.isfile(cand):
                            video_path = cand
                        else:
                            base_matches = glob.glob(os.path.join(artifact_dir, "**", os.path.basename(video_rel)), recursive=True)
                            if base_matches:
                                video_path = base_matches[0]
                    if not video_path:
                        v_matches = glob.glob(os.path.join(artifact_dir, "**", f"*chapter_{chap_num}.mp4"), recursive=True)
                        if v_matches:
                            video_path = v_matches[0]

                    # Resolve subtitle file path
                    srt_rel = entry.get("srt_relpath")
                    srt_path = None
                    if srt_rel:
                        cand_srt = os.path.join(artifact_dir, srt_rel)
                        if os.path.isfile(cand_srt):
                            srt_path = cand_srt
                        else:
                            base_srt_matches = glob.glob(os.path.join(artifact_dir, "**", os.path.basename(srt_rel)), recursive=True)
                            if base_srt_matches:
                                srt_path = base_srt_matches[0]
                    if not srt_path and video_path:
                        srt_cand = video_path.replace("/Video/", "/Subtitles/").replace(os.sep + "Video" + os.sep, os.sep + "Subtitles" + os.sep).replace(".mp4", ".srt")
                        if os.path.isfile(srt_cand):
                            srt_path = srt_cand
                        else:
                            s_matches = glob.glob(os.path.join(artifact_dir, "**", f"*chapter_{chap_num}.srt"), recursive=True)
                            if s_matches:
                                srt_path = s_matches[0]

                    # Fallback probing if manifest duration is missing/zero
                    if dur <= 0 and video_path:
                        dur = media_dur_fn(video_path)
                    if dur <= 0 and srt_path:
                        dur = duration_from_srt(srt_path)

                    if video_path:
                        chapters.append({
                            "artifact": entry.get("artifact") or artifact_name,
                            "chap_num": chap_num,
                            "chapter_title": entry.get("chapter_title") or f"第{chap_num}章",
                            "dur": dur,
                            "path": os.path.abspath(video_path),
                            "srt_path": os.path.abspath(srt_path) if srt_path else None,
                            "video_relpath": os.path.relpath(video_path, artifact_dir),
                            "srt_relpath": os.path.relpath(srt_path, artifact_dir) if srt_path else None,
                        })
                if chapters:
                    return sorted(chapters, key=lambda item: item["chap_num"])
        except Exception as err:
            logging.debug("Failed parsing manifest %s: %s", mf, err)

    # 2. Fallback: file traversal
    chapters = []
    for video_path in glob.glob(os.path.join(artifact_dir, "**", "*.mp4"), recursive=True):
        chapter_num = parse_num_fn(os.path.basename(video_path))
        if chapter_num == 999999:
            continue
        srt_path = video_path.replace("/Video/", "/Subtitles/").replace(os.sep + "Video" + os.sep, os.sep + "Subtitles" + os.sep).replace(".mp4", ".srt")
        if not os.path.exists(srt_path):
            srt_matches = glob.glob(
                os.path.join(artifact_dir, "**", f"*chapter_{chapter_num}.srt"),
                recursive=True,
            )
            srt_path = srt_matches[0] if srt_matches else None
        dur = media_dur_fn(video_path)
        if dur <= 0 and srt_path:
            dur = duration_from_srt(srt_path)
        chapters.append({
            "artifact": artifact_name,
            "chap_num": chapter_num,
            "chapter_title": f"第{chapter_num}章",
            "dur": dur,
            "path": os.path.abspath(video_path),
            "srt_path": os.path.abspath(srt_path) if srt_path else None,
            "video_relpath": os.path.relpath(video_path, artifact_dir),
            "srt_relpath": os.path.relpath(srt_path, artifact_dir) if srt_path else None,
        })
    return sorted(chapters, key=lambda item: item["chap_num"])


def _validate_complete_chapter_inventory(chapters, expected_start, expected_end):
    """Refuse to upload unless the complete book is present exactly once."""
    numbers = [int(item["chap_num"]) for item in chapters]
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    expected = list(range(int(expected_start), int(expected_end) + 1))
    missing = sorted(set(expected) - set(numbers))
    unexpected = sorted(set(numbers) - set(expected))
    if duplicates or missing or unexpected or numbers != sorted(numbers):
        raise RuntimeError(
            "章節盤點不完整，禁止開始上傳："
            f"重複={duplicates[:10]}，缺少={missing[:10]}，超出範圍={unexpected[:10]}"
        )


def validate_chapter_inventory(chapters, expected_start, expected_end, confirmed_missing=None):
    """Accept absent chapters only when worker artifacts prove origin omission."""
    confirmed_missing = {int(value) for value in (confirmed_missing or set())}
    numbers = [int(item["chap_num"]) for item in chapters]
    duplicates = sorted({number for number in numbers if numbers.count(number) > 1})
    expected = set(range(int(expected_start), int(expected_end) + 1))
    missing = sorted(expected - set(numbers))
    unresolved = sorted(set(missing) - confirmed_missing)
    unexpected = sorted(set(numbers) - expected)
    if duplicates or unresolved or unexpected or numbers != sorted(numbers):
        raise RuntimeError(
            "chapter inventory is not publishable: "
            f"duplicates={duplicates[:10]}, unresolved_missing={unresolved[:10]}, "
            f"unexpected={unexpected[:10]}"
        )
    return {"missing": missing, "confirmed_missing": sorted(set(missing) & confirmed_missing)}


def build_part_plan_from_inventory(chapters, min_seconds=10 * 3600, max_seconds=11 * 3600,
                                   confirmed_missing=None):
    """Plan every Part globally before any merge or YouTube API upload."""
    if not chapters:
        raise RuntimeError("沒有可供分部的章節")
    ordered = sorted(chapters, key=lambda item: int(item["chap_num"]))
    plan = []
    current = []
    current_duration = 0.0
    for item in ordered:
        duration = float(item["dur"])
        if duration <= 0:
            raise RuntimeError(f"第 {item['chap_num']} 章無法取得有效片長")
        if current and current_duration + duration > max_seconds:
            plan.append(_make_planned_part(len(plan) + 1, current, current_duration))
            current = []
            current_duration = 0.0
        current.append(item)
        current_duration += duration
    if current:
        plan.append(_make_planned_part(len(plan) + 1, current, current_duration))

    confirmed_missing = {int(value) for value in (confirmed_missing or set())}
    for previous, following in zip(plan, plan[1:]):
        gap = set(range(previous["end_chap"] + 1, following["start_chap"]))
        if gap and not gap.issubset(confirmed_missing):
            raise RuntimeError(f"分部不連續：Part {previous['part_num']} 後接 Part {following['part_num']}")
    unassigned_missing = set(confirmed_missing)
    for part in plan:
        assigned = {chapter for chapter in unassigned_missing if chapter <= part["end_chap"]}
        part["source_missing_chapters"] = sorted(assigned)
        unassigned_missing.difference_update(assigned)
    if plan and unassigned_missing:
        plan[-1]["source_missing_chapters"].extend(sorted(unassigned_missing))
    for part in plan[:-1]:
        if part["duration"] < min_seconds:
            logging.warning("Part %s 只有 %.2f 小時；受 11 小時硬上限約束。", part["part_num"], part["duration"] / 3600)
    return plan


def _make_planned_part(part_num, items, duration):
    return {
        "part_num": part_num,
        "start_chap": int(items[0]["chap_num"]),
        "end_chap": int(items[-1]["chap_num"]),
        "chapters": [int(item["chap_num"]) for item in items],
        "chapter_timeline": [
            {
                "chap_num": int(item["chap_num"]),
                "chapter_title": _chapter_title(item),
                "dur": float(item["dur"]),
            }
            for item in items
        ],
        "artifacts": list(dict.fromkeys(item["artifact"] for item in items)),
        "duration": duration,
    }


def download_artifact_task(run_id, repo, artifact_name, dest_dir):
    subproc_run = _get_symbol("subprocess", subprocess).run
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)

    dl_cmd = [
        _find_gh(), "run", "download", str(run_id),
        "--repo", repo,
        "--name", artifact_name,
        "--dir", dest_dir,
    ]
    res = subproc_run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.returncode == 0


def get_latest_successful_run_id(repo):
    """Fallback when no run_id is passed and no state.json exists: auto-detect latest video production run."""
    subproc_run = _get_symbol("subprocess", subprocess).run
    try:
        cmd = [
            _find_gh(), "api",
            f"repos/{repo}/actions/workflows/audiobook.yml/runs?status=success&per_page=1",
            "--jq", ".workflow_runs[0].id",
        ]
        res = subproc_run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout.strip() and res.stdout.strip() != "null":
            return res.stdout.strip()
    except Exception as e:
        logging.warning("無法自動查詢最新 Run ID: %s", e)
    return None
