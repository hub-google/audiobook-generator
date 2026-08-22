"""Durable per-book settings stored on the GitHub automation-state branch."""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

try:
    from .cloud_queue import GitHubQueueStore
except ImportError:
    from cloud_queue import GitHubQueueStore


PROFILE_PATH = "audiobook-book-profiles.json"
PROFILE_SCHEMA_VERSION = 1
MAX_PATTERNS = 100
MAX_PATTERN_LENGTH = 500


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_catalog_url(value):
    raw = str(value or "").strip()
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("小說目錄網址無效")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def book_profile_id(catalog_url):
    return hashlib.sha256(normalize_catalog_url(catalog_url).encode("utf-8")).hexdigest()[:16]


def validate_remove_patterns(patterns):
    result = []
    for raw in patterns or []:
        pattern = str(raw).strip()
        if not pattern or pattern in result:
            continue
        if len(pattern) > MAX_PATTERN_LENGTH:
            raise ValueError(f"刪除關鍵字不可超過 {MAX_PATTERN_LENGTH} 個字元")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"正則表達式無效：{pattern}（{error}）") from error
        if compiled.search(""):
            raise ValueError(f"刪除規則不可匹配空字串：{pattern}")
        result.append(pattern)
    if len(result) > MAX_PATTERNS:
        raise ValueError(f"每本小說最多 {MAX_PATTERNS} 條刪除規則")
    return result


def empty_profiles():
    return {"schema_version": PROFILE_SCHEMA_VERSION, "revision": 0, "updated_at": utc_now(), "books": {}}


def normalize_profiles(value):
    data = copy.deepcopy(value) if isinstance(value, dict) else empty_profiles()
    version = int(data.get("schema_version") or 1)
    if version > PROFILE_SCHEMA_VERSION:
        raise ValueError(f"書籍設定格式版本 {version} 高於本程式支援的 {PROFILE_SCHEMA_VERSION}，請先更新 GUI")
    books = data.setdefault("books", {})
    if not isinstance(books, dict):
        raise ValueError("book profiles books 必須是物件")
    for key, profile in list(books.items()):
        if not isinstance(profile, dict):
            raise ValueError(f"書籍設定 {key} 格式錯誤")
        profile["catalog_url"] = normalize_catalog_url(profile.get("catalog_url"))
        expected = book_profile_id(profile["catalog_url"])
        if key != expected:
            books[expected] = books.pop(key)
            key = expected
            profile = books[key]
        profile["cleaner_remove_patterns"] = validate_remove_patterns(profile.get("cleaner_remove_patterns"))
        detection = profile.setdefault("duplicate_detection", {})
        profile["duplicate_detection"] = {
            "use_normalized_number": bool(detection.get("use_normalized_number", True)),
            "use_chapter_name": bool(detection.get("use_chapter_name", True)),
        }
        profile.setdefault("chapter_title_overrides", {})
        profile.setdefault("profile_revision", 1)
    data["schema_version"] = PROFILE_SCHEMA_VERSION
    data.setdefault("revision", 0)
    data.setdefault("updated_at", utc_now())
    return data


def get_book_profile(data, catalog_url, book_title=""):
    data = normalize_profiles(data)
    normalized_url = normalize_catalog_url(catalog_url)
    key = book_profile_id(normalized_url)
    profile = copy.deepcopy(data["books"].get(key) or {
        "catalog_url": normalized_url,
        "book_title": str(book_title or "待解析"),
        "cleaner_remove_patterns": [],
        "duplicate_detection": {"use_normalized_number": True, "use_chapter_name": True},
        "chapter_title_overrides": {},
        "profile_revision": 0,
    })
    return key, profile


def update_book_profile(data, catalog_url, book_title="", cleaner_remove_patterns=None,
                        duplicate_detection=None, chapter_title_overrides=None):
    data = normalize_profiles(data)
    key, profile = get_book_profile(data, catalog_url, book_title)
    changed = False
    changes = {
        "book_title": str(book_title or profile.get("book_title") or "待解析"),
    }
    if cleaner_remove_patterns is not None:
        changes["cleaner_remove_patterns"] = validate_remove_patterns(cleaner_remove_patterns)
    if duplicate_detection is not None:
        changes["duplicate_detection"] = {
            "use_normalized_number": bool(duplicate_detection.get("use_normalized_number", True)),
            "use_chapter_name": bool(duplicate_detection.get("use_chapter_name", True)),
        }
    if chapter_title_overrides is not None:
        changes["chapter_title_overrides"] = {
            str(int(k)): str(v) for k, v in chapter_title_overrides.items()
            if str(k).isdigit() and str(v).strip()
        }
    for name, value in changes.items():
        if profile.get(name) != value:
            profile[name] = value
            changed = True
    if changed or key not in data["books"]:
        profile["profile_revision"] = int(profile.get("profile_revision") or 0) + 1
        profile["updated_at"] = utc_now()
        data["books"][key] = profile
        data["revision"] = int(data.get("revision") or 0) + 1
        data["updated_at"] = utc_now()
    return data


def profile_snapshot(profile_id, profile):
    snapshot = {
        "book_profile_id": profile_id,
        "profile_revision": int(profile.get("profile_revision") or 0),
        "cleaner_remove_patterns": validate_remove_patterns(profile.get("cleaner_remove_patterns")),
        "duplicate_detection": copy.deepcopy(profile.get("duplicate_detection") or {}),
        "chapter_title_overrides": copy.deepcopy(profile.get("chapter_title_overrides") or {}),
    }
    canonical = json.dumps(snapshot["cleaner_remove_patterns"], ensure_ascii=False, separators=(",", ":"))
    snapshot["cleaner_fingerprint"] = hashlib.sha256(("cleaner-v2|" + canonical).encode("utf-8")).hexdigest()
    return snapshot


class GitHubBookProfileStore(GitHubQueueStore):
    def __init__(self, repo, token, branch="automation-state", timeout=20):
        super().__init__(repo, token, branch=branch, path=PROFILE_PATH, timeout=timeout)

    def load(self):
        self.ensure_branch()
        response = self._request("GET", f"{self.base}/contents/{self.path}", params={"ref": self.branch})
        if response.status_code == 404:
            return empty_profiles(), None
        if response.status_code != 200:
            raise RuntimeError(f"GitHub profile read failed ({response.status_code}): {response.text}")
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        return normalize_profiles(json.loads(content)), payload["sha"]

    def save(self, profiles, sha=None, message="Update audiobook book profiles"):
        # GitHubQueueStore normalizes queue-shaped data, so profiles use the same
        # SHA-guarded Contents API transport without its queue serializer.
        self.ensure_branch()
        data = normalize_profiles(profiles)
        body = {
            "message": message,
            "branch": self.branch,
            "content": base64.b64encode(
                (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            ).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        response = self._request("PUT", f"{self.base}/contents/{self.path}", json=body)
        if response.status_code in (409, 422):
            try:
                from .cloud_queue import QueueConflict
            except ImportError:
                from cloud_queue import QueueConflict
            raise QueueConflict("book profiles changed remotely; reload before saving")
        if response.status_code not in (200, 201):
            raise RuntimeError(f"GitHub profile write failed ({response.status_code}): {response.text}")
        return response.json()["content"]["sha"]

    def mutate(self, callback, message, attempts=4):
        from time import sleep
        try:
            from .cloud_queue import QueueConflict
        except ImportError:
            from cloud_queue import QueueConflict
        for attempt in range(attempts):
            data, sha = self.load()
            updated = callback(data)
            try:
                self.save(updated, sha=sha, message=message)
                return normalize_profiles(updated)
            except QueueConflict:
                if attempt + 1 == attempts:
                    raise
                sleep(0.5 * (attempt + 1))
