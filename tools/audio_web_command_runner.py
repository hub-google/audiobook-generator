"""Secure AUDIO site-command runner: only GitHub Actions obtains repository credentials.

Commands are submitted to an authenticated Supabase Edge Function and claimed
with a GitHub OIDC assertion scoped to this repo and workflow, not a shared
password. Queue/profile writes reuse the existing validated command handler.
"""
import os
import tempfile
from pathlib import Path

import requests
from src.book_profiles import GitHubBookProfileStore, book_profile_id, update_book_profile
from src.cloud_queue import GitHubQueueStore
from src.cover_assets import normalize_manual_cover, upload_github_cover
from tools.audio_issue_command import api, apply_command, valid_id

# All commands remain in the site; GitHub OIDC verifies the processing workflow.
REPO = "hub-google/audiobook-generator"
EDGE = "https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-web-commands"


def runner_jwt():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    glue = "&" if "?" in url else "?"
    response = requests.get(
        url + glue + "audience=audio-web-commands",
        headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["value"]


def edge(method, path, *, payload=None, binary=False):
    # Fetch a fresh, short-lived OIDC token for every privileged exchange.
    response = requests.request(
        method, EDGE + path,
        headers={"Authorization": "Bearer " + runner_jwt(), "Accept": "application/json"},
        json=payload,
        timeout=75,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"AUDIO command service rejected {path} HTTP {response.status_code}")
    return response.content if binary else response.json()


def manual_cover(data, issue_number, token):
    task_id = valid_id(data.get("task_id"))
    key = str(data.get("cover_key") or "")
    if len(key) > 180 or not key.endswith(".jpg") or ".." in key:
        raise ValueError("Invalid cover storage key")
    state, _ = GitHubQueueStore(REPO, token).load()
    task = next((t for t in state.get("queue", []) if t.get("task_id") == task_id), None)
    if task is None or task.get("workflow_phase") not in ("queued", "preflight", None):
        raise ValueError("Task missing or no longer allows cover replacement")
    if task.get("status") in ("processing", "running", "completed"):
        raise ValueError("Cover already locked for video production")
    from urllib.parse import quote
    jpeg = edge("GET", "/cover?key=" + quote(key, safe=""), binary=True)
    if not jpeg.startswith(b"\xff\xd8") or len(jpeg) > 10_000_000:
        raise ValueError("Invalid cover image")
    with tempfile.TemporaryDirectory(prefix="audio-web-cover-") as folder:
        raw = Path(folder) / "upload.jpg"
        processed = Path(folder) / "master_cover.jpg"
        raw.write_bytes(jpeg)
        details = normalize_manual_cover(raw, processed)
        cloud = upload_github_cover(processed, book_profile_id(task["catalog_url"]), REPO, token)
    record = {"source": "manual_upload", **cloud, **details}
    GitHubBookProfileStore(REPO, token).mutate(
        lambda profiles: update_book_profile(
            profiles, task["catalog_url"], task["book_title"], manual_cover=record
        ),
        f"AUDIO manual cover web command #{issue_number}",
    )
    return "手動封面已寫入小說設定"


def process(row, gh_token):
    request_number = 1_000_000_000 + int(row["id"])
    op = row["op"]
    values = row["payload"]
    if not isinstance(values, dict):
        raise ValueError("Invalid operation data")
    if op == "manual_cover":
        return manual_cover(values, request_number, gh_token)
    store = GitHubQueueStore(REPO, gh_token)
    profiles = GitHubBookProfileStore(REPO, gh_token)
    message, dispatch = apply_command(
        {"version": 1, "op": op, "data": values},
        store, profiles, request_number, gh_token, REPO,
    )
    if dispatch:
        api(
            "POST", "actions/workflows/queue-dispatcher.yml/dispatches", gh_token, REPO,
            json={"ref": "main", "inputs": {"force_retry": "false"}},
        )
    return message


def main():
    token = os.environ["GITHUB_TOKEN"]
    rows = edge("POST", "/claim", payload={}).get("commands") or []
    print(f"AUDIO web runner: {len(rows)} claim(s)")
    failed = 0
    for row in rows:
        job_id = row["id"]
        try:
            outcome = process(row, token)
            edge("POST", "/finish", payload={"id": job_id, "ok": True, "result": outcome})
            print(f"Command #{job_id}: completed {row['op']}")
        except Exception as exc:
            failed += 1
            print(f"Command #{job_id}: failed {row['op']} ({type(exc).__name__})")
            try:
                edge("POST", "/finish", payload={
                    "id": job_id, "ok": False, "result": "",
                    "error": type(exc).__name__,
                })
            except Exception:
                print("Could not acknowledge failed operation; it will be retried")
    if failed:
        raise SystemExit(f"{failed} AUDIO site command(s) failed")


if __name__ == "__main__":
    main()
