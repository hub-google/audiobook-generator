"""Verify user-reviewed cover media against the actual authenticated Action Artifact.

The browser submits hashes, never a GitHub token. The server downloads the same
cover-review Artifact which production restores, checks the manifest, and
compares both files before allowing processing. No novel text is made public.
"""
import hashlib
import json
import re
import tempfile
import zipfile

import requests


def validate_cover_review_evidence(values, task, token, repo, api):
    supplied = values.get("cover_evidence") or {}
    if not isinstance(supplied, dict) or supplied.get("task_id") != task["task_id"]:
        raise ValueError("Cover evidence task ID does not match this book")
    for key in ("cover_sha256", "prompt_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(supplied.get(key) or "")):
            raise ValueError("Cover approval requires both SHA-256 fingerprints")

    run_id = task.get("cover_run_id")
    if not str(run_id or "").isdigit():
        raise ValueError("Cover preflight Run has not been recorded")
    result = api("GET", f"actions/runs/{run_id}/artifacts?per_page=100", token, repo)
    matched = [
        item for item in result.get("artifacts", [])
        if not item.get("expired")
        and item.get("name") == f"cover-review-{task['task_id']}"
    ]
    if len(matched) != 1:
        raise ValueError("Matching cover-review Artifact is missing or ambiguous")

    with tempfile.SpooledTemporaryFile(max_size=32*1024*1024) as cache:
        total = 0
        with requests.get(matched[0]["archive_download_url"],
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json"},
                          stream=True, timeout=(15, 180)) as response:
            response.raise_for_status()
            for part in response.iter_content(chunk_size=1024*1024):
                if not part:
                    continue
                total += len(part)
                if total > 80*1024*1024:
                    raise ValueError("Cover Artifact ZIP exceeds safe size")
                cache.write(part)
        cache.seek(0)
        with zipfile.ZipFile(cache) as archive:
            def one(filename):
                names = [n for n in archive.namelist()
                         if n.rsplit("/", 1)[-1] == filename and not n.endswith("/")]
                if len(names) != 1:
                    raise ValueError("Missing or ambiguous "+filename)
                entry = archive.getinfo(names[0])
                if entry.file_size > 50*1024*1024:
                    raise ValueError("Cover Artifact entry is too large")
                return archive.read(entry)
            manifest = json.loads(one("cover-preflight-manifest.json"))
            if (manifest.get("artifact_role") != "cover_preflight"
                    or manifest.get("schema_version") != 1
                    or manifest.get("task_id") != task["task_id"]):
                raise ValueError("Cover manifest task identity mismatch")
            cover_hash = hashlib.sha256(one("master_cover.jpg")).hexdigest()
            prompt_hash = hashlib.sha256(one("master_cover_prompt.json")).hexdigest()
            if (manifest.get("cover_sha256") != cover_hash or
                    manifest.get("prompt_sha256") != prompt_hash):
                raise ValueError("Cover Artifact manifest checksum mismatch")
            if (supplied["cover_sha256"] != cover_hash or
                    supplied["prompt_sha256"] != prompt_hash):
                raise ValueError("Reviewed cover differs from the actual GitHub Artifact")

    return {"cover_verified_sha256": cover_hash,
            "cover_prompt_verified_sha256": prompt_hash}
