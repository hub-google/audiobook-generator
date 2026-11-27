"""Validate a book's TXT review against its actual GitHub scrape artifact."""
import json
import re
import tempfile
import zipfile
import requests
from src.book_profiles import validate_remove_patterns


def validate_review_evidence(values, task, token, repo, api):
    evidence = values.get("evidence") or {}
    supplied = str(evidence.get("raw_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise ValueError("Review requires the actual raw text fingerprint")
    if evidence.get("task_id") != task["task_id"]:
        raise ValueError("TXT report task identity differs from selected book")
    run_id = task.get("scrape_run_id")
    if not str(run_id or "").isdigit():
        raise ValueError("TXT Run has not been recorded")
    result = api("GET", f"actions/runs/{run_id}/artifacts?per_page=100", token, repo)
    items = [
        item for item in (result.get("artifacts") or [])
        if not item.get("expired") and
        str(item.get("name") or "").startswith(f"scrape-review-{task['task_id']}")
    ]
    if len(items) != 1:
        raise ValueError("Matching TXT report Artifact missing or ambiguous")
    with tempfile.SpooledTemporaryFile(max_size=32*1024*1024) as cache:
        with requests.get(items[0]["archive_download_url"],
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json"},
                          timeout=(15,180), stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk:
                    cache.write(chunk)
        cache.seek(0)
        with zipfile.ZipFile(cache) as archive:
            names = [name for name in archive.namelist()
                     if name.endswith(".json") and "ad-candidates-" in name]
            if len(names) != 1:
                raise ValueError("TXT report JSON missing or ambiguous")
            report = json.loads(archive.read(names[0]))
    if (report.get("artifact_role") != "ad_review_candidates" or
            report.get("task_id") != task["task_id"] or
            report.get("raw_fingerprint") != supplied):
        raise ValueError("TXT review result or raw fingerprint does not match GitHub Artifact")
    removed = validate_remove_patterns(values.get("remove_patterns") or [])
    candidates = [
        item["text"] for item in (report.get("candidates") or [])
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return {
        "raw_fingerprint": supplied,
        "scrape_run_id": task["scrape_run_id"],
        "cover_run_id": task.get("cover_run_id"),
        "remove_patterns": removed,
        "keep_patterns": list(dict.fromkeys(t for t in candidates if t not in removed)),
    }
