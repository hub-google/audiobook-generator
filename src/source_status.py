"""Durable classification for chapter pages that contain no source article."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone


ANTI_BOT_MARKERS = (
    "captcha", "cloudflare", "just a moment", "access denied",
    "too many requests", "robot check", "人機驗證", "訪問過於頻繁",
)


class SourceMissingError(RuntimeError):
    """The origin page was reached repeatedly but has no chapter article."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def response_fingerprint(content):
    if isinstance(content, str):
        content = content.encode("utf-8", errors="replace")
    return hashlib.sha256(content or b"").hexdigest()


def looks_like_anti_bot_page(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ANTI_BOT_MARKERS)


class SourceStatusStore:
    """One atomic JSON file per chapter avoids cross-worker write collisions."""

    def __init__(self, workspace_dir):
        self.directory = os.path.join(os.path.abspath(workspace_dir), "SourceStatus")
        os.makedirs(self.directory, exist_ok=True)

    def path(self, chapter):
        return os.path.join(self.directory, f"chapter-{int(chapter)}.json")

    def load(self, chapter):
        path = self.path(chapter)
        if not os.path.exists(path):
            return {"chapter": int(chapter), "status": "pending", "observations": []}
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"chapter": int(chapter), "status": "pending", "observations": []}

    def is_confirmed_missing(self, chapter):
        return self.load(chapter).get("status") == "source_missing"

    def record_empty_page(self, chapter, url, status_code, final_url, title, content,
                          required_confirmations=3):
        data = self.load(chapter)
        fingerprint = response_fingerprint(content)
        observation = {
            "observed_at": _now(),
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
            "http_status": int(status_code),
            "url": url,
            "final_url": final_url,
            "title": title,
            "fingerprint": fingerprint,
        }
        observations = list(data.get("observations") or [])
        observations.append(observation)
        observations = observations[-20:]
        data.update({
            "chapter": int(chapter),
            "status": "source_missing_candidate",
            "reason": "origin page returned successfully but contained no chapter article",
            "observations": observations,
            "updated_at": _now(),
        })
        matching = [item for item in observations if (
            item.get("http_status") == 200
            and item.get("fingerprint") == fingerprint
            and item.get("final_url") == final_url
        )]
        if len(matching) >= int(required_confirmations):
            data["status"] = "source_missing"
            data["confirmed_at"] = _now()
            data["confirmation_count"] = len(matching)
        self.save(chapter, data)
        return data

    def mark_available(self, chapter):
        data = self.load(chapter)
        if data.get("status") == "source_missing":
            data["previous_status"] = "source_missing"
        data.update({"status": "available", "available_at": _now(), "updated_at": _now()})
        self.save(chapter, data)

    def save(self, chapter, data):
        path = self.path(chapter)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)


def confirmed_missing_from_directory(root):
    """Read confirmed chapter numbers from an extracted worker artifact."""
    confirmed = set()
    for directory, _, filenames in os.walk(root):
        if os.path.basename(directory) != "SourceStatus":
            continue
        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, filename), encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("status") == "source_missing":
                    confirmed.add(int(data["chapter"]))
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return confirmed
