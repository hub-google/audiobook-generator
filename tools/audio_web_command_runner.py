"""Secure AUDIO site-command runner: only GitHub Actions obtains repository credentials.

Commands are submitted to an authenticated Supabase Edge Function and claimed
with a GitHub OIDC assertion scoped to this repo and workflow, not a shared
password. Queue/profile writes reuse the existing validated command handler.
"""
import os
import tempfile
import io
import re
import json
import base64
import zipfile
from pathlib import Path

import requests
from src.book_profiles import GitHubBookProfileStore, book_profile_id, update_book_profile
from src.cloud_queue import GitHubQueueStore
from src.cover_assets import normalize_manual_cover, upload_github_cover
from tools.audio_issue_command import api, apply_command, valid_id, valid_url

# All commands remain in the site; GitHub OIDC verifies the processing workflow.
# Live integration smoke: download one existing tiny Artifact and its Run logs with protected staging.
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


def _valid_task_run(task_id, run_id, token):
    """Only fetch assets from Runs attached to an existing AUDIO queue task."""
    valid_id(task_id)
    if not isinstance(run_id, int) or not (0 < run_id < 10**15):
        raise ValueError("Invalid GitHub Run ID")
    state, _ = GitHubQueueStore(REPO, token).load()
    task = next((t for t in state.get("queue", []) + state.get("completed", [])
                 if t.get("task_id") == task_id), None)
    if not task:
        raise ValueError("The book is no longer in the AUDIO queue")
    allowed = {
        int(t[key]) for t in [task]
        for key in ("run_id", "scrape_run_id", "cover_run_id", "processing_run_id")
        if str(t.get(key) or "").isdigit() and int(t[key]) > 0
    }
    for row in task.get("run_history") or []:
        if str(row.get("run_id") or "").isdigit():
            allowed.add(int(row["run_id"]))
    for item in task.get("stages", {}).values():
        if isinstance(item, dict) and str(item.get("run_id") or "").isdigit():
            allowed.add(int(item["run_id"]))
    if run_id not in allowed:
        raise PermissionError("Run does not belong to the selected AUDIO book")
    return task


def _download_run_bytes(path, token, limit=45_000_000):
    url = "https://api.github.com/repos/" + REPO + "/" + path
    with requests.get(
        url,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        stream=True, allow_redirects=True, timeout=(20, 90),
    ) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > limit:
            raise ValueError("GitHub Artifact exceeds the single-file 45 MB upload limit")
        chunks, size = [], 0
        for piece in response.iter_content(chunk_size=128_000):
            if not piece:
                continue
            size += len(piece)
            if size > limit:
                raise ValueError("GitHub Artifact exceeds the single-file 45 MB upload limit")
            chunks.append(piece)
        return b"".join(chunks)


def _stage(id_number, filename, content_type, content):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", filename):
        raise ValueError("Unsafe artifact filename")
    if not content or len(content) > 45_000_000:
        raise ValueError("File empty or too large for protected AUDIO staging")
    result = requests.post(
        EDGE + "/stage",
        data=content,
        headers={"Authorization": "Bearer " + runner_jwt(),
                 "Content-Type": content_type,
                 "X-Audio-Command-Id": str(id_number),
                 "X-Audio-Filename": filename},
        timeout=(20, 120),
    )
    result.raise_for_status()
    data = result.json()
    if not data.get("ok"):
        raise RuntimeError("Protected asset staging did not return success")
    return data["asset"]["id"]


def fetch_artifact(row, token):
    payload = row["payload"]
    task_id = valid_id(payload.get("task_id"))
    run_id = payload.get("run_id")
    if not isinstance(run_id, int):
        raise ValueError("Invalid Run ID")
    _valid_task_run(task_id, run_id, token)
    kind = payload.get("kind")
    if kind not in ("scrape", "cover", "export", "any"):
        raise ValueError("Invalid Artifact category")
    prefix = {
        "scrape": "scrape-review-" + task_id,
        "cover": "cover-review-" + task_id,
        "export": "audiobook-",
        "any": "",
    }[kind]
    selected = payload.get("artifact_id")
    artifact_data = api("GET", f"actions/runs/{run_id}/artifacts?per_page=100", token, REPO)
    matching = [
        item for item in artifact_data.get("artifacts") or []
        if not item.get("expired") and (selected is None or item.get("id") == selected)
        and str(item.get("name") or "").startswith(prefix)
    ]
    if not matching:
        raise ValueError("No matching, non-expired GitHub Artifact exists for this book")
    item = sorted(matching, key=lambda a: a.get("created_at", ""), reverse=True)[0]
    if int(item.get("size_in_bytes") or 0) > 45_000_000:
        raise ValueError("Artifact larger than 45 MB: requires a future chunked download flow")
    content = _download_run_bytes(f"actions/artifacts/{item['id']}/zip", token)
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise RuntimeError("GitHub Artifact was not a valid ZIP")
    name = f"audio-{kind}-run-{run_id}-artifact-{item['id']}.zip"
    asset_id = _stage(row["id"], name, "application/zip", content)
    return f"已取得 {item.get('name')}（{len(content)} bytes）；站內檔案編號 {asset_id}"


def fetch_logs(row, token):
    payload = row["payload"]
    task_id = valid_id(payload.get("task_id"))
    run_id = payload.get("run_id")
    if not isinstance(run_id, int):
        raise ValueError("Invalid Run ID")
    _valid_task_run(task_id, run_id, token)
    raw = _download_run_bytes(f"actions/runs/{run_id}/logs", token, limit=30_000_000)
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise RuntimeError("Run logs were not a ZIP archive")
    selected = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            if entry.is_dir() or not entry.filename.endswith(".txt"):
                continue
            if entry.file_size > 5_000_000:
                continue
            with archive.open(entry) as stream:
                body = stream.read(5_000_001)
            if len(body) > 5_000_000:
                continue
            title = f"\n===== {entry.filename} =====\n"
            excerpt = body.decode("utf-8", errors="replace")
            total += len(title) + len(excerpt)
            if total > 9_000_000:
                break
            selected.append(title + excerpt)
    if not selected:
        raise ValueError("No readable log entries found for this Run")
    result = "\n".join(selected)
    output = result[-8_500_000:].encode("utf-8")
    asset_id = _stage(row["id"], f"audio-run-{run_id}-logs.txt", "text/plain", output)
    return f"已取得 Run {run_id} 的原始步驟日誌（{len(output)} bytes）；站內檔案編號 {asset_id}"


def _draft_cover_data(values):
    url=valid_url(values.get("catalog_url"))
    title=str(values.get("book_title") or "").strip()
    if not title or len(title)>160:
        raise ValueError("Draft cover requires a real book title")
    return url,title


def manual_cover_prequeue(row, token):
    values=row["payload"]
    url,title=_draft_cover_data(values)
    key=str(values.get("cover_key") or "")
    if len(key)>180 or not re.fullmatch(r"[0-9a-f-]{36}/[0-9a-f-]{36}\.jpg",key):
        raise ValueError("Invalid draft cover key")
    from urllib.parse import quote
    content=edge("GET","/cover?key="+quote(key,safe=""),binary=True)
    if not content.startswith(b"\xff\xd8") or len(content)>10_000_000:
        raise ValueError("Invalid JPEG")
    with tempfile.TemporaryDirectory(prefix="audio-draft-cover-") as folder:
        raw=Path(folder)/"input.jpg"
        final=Path(folder)/"master_cover.jpg"
        raw.write_bytes(content)
        details=normalize_manual_cover(raw,final)
        cloud=upload_github_cover(final,book_profile_id(url),REPO,token)
    record={"source":"manual_upload",**cloud,**details}
    GitHubBookProfileStore(REPO,token).mutate(
        lambda profiles: update_book_profile(profiles,url,title,manual_cover=record),
        "AUDIO prequeue manual cover for "+title[:70],
    )
    return "已為《"+title+"》儲存封面；新增小說後會自動沿用"


def cover_info_prequeue(row, token):
    url,title=_draft_cover_data(row["payload"])
    from src.metadata_gen import build_cover_information
    path="cover-information/"+book_profile_id(url)+".json"
    force=bool(row["payload"].get("force"))
    cached=None
    if not force:
        try:
            remote=api("GET","contents/"+path+"?ref=automation-state",token,REPO)
            cached=json.loads(base64.b64decode(remote["content"]).decode("utf-8"))
        except (RuntimeError,KeyError,ValueError):
            cached=None
    if isinstance(cached,dict) and cached.get("prompt"):
        result=cached
    else:
        result=build_cover_information(title,catalog_url=url)
        if not isinstance(result,dict) or not result.get("prompt"):
            raise RuntimeError("Gemini metadata did not return a usable Prompt")
        contents=json.dumps(result,ensure_ascii=False,indent=2).encode("utf-8")
        sha=None
        try:
            existing=api("GET","contents/"+path+"?ref=automation-state",token,REPO)
            sha=existing.get("sha")
        except RuntimeError:
            pass
        body={"message":"AUDIO store verified cover information for "+title[:60],
              "content":base64.b64encode(contents).decode("ascii"),"branch":"automation-state"}
        if sha:body["sha"]=sha
        api("PUT","contents/"+path,token,REPO,json=body)
    encoded=json.dumps(result,ensure_ascii=False,indent=2).encode("utf-8")
    asset=_stage(row["id"],"audio-cover-information-"+str(row["id"])+".txt","text/plain",encoded)
    return "已取得《"+title+"》封面資訊，站內檔案編號 "+str(asset)


def process(row, gh_token):
    request_number = 1_000_000_000 + int(row["id"])
    op = row["op"]
    values = row["payload"]
    if not isinstance(values, dict):
        raise ValueError("Invalid operation data")
    if op == "manual_cover":
        return manual_cover(values, request_number, gh_token)
    if op == "fetch_artifact":
        return fetch_artifact(row, gh_token)
    if op == "fetch_logs":
        return fetch_logs(row, gh_token)
    if op == "manual_cover_prequeue":
        return manual_cover_prequeue(row, gh_token)
    if op == "cover_info_prequeue":
        return cover_info_prequeue(row, gh_token)
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
