"""Read mergeable audiobook books and Parts from the HF archive dataset."""
from __future__ import annotations

import json
import os
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def load_local_env(root: Path) -> None:
    path = root / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def duration_seconds(media: dict[str, Any]) -> float:
    try:
        return float((media.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class HfPart:
    number: int
    start_chapter: int
    end_chapter: int
    video_path: str
    video_bytes: int
    video_sha256: str
    duration: float
    chapter_timeline: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HfBook:
    key: str
    title: str
    root: str
    revision: str
    parts: list[HfPart] = field(default_factory=list)
    error: str = ""

    @property
    def total_duration(self) -> float:
        return sum(item.duration for item in self.parts)

    @property
    def total_bytes(self) -> int:
        return sum(item.video_bytes for item in self.parts)

    @property
    def mergeable(self) -> bool:
        return bool(self.parts) and not self.error


class HfCatalog:
    def __init__(self, repo_id: str, token: str):
        if not repo_id:
            raise ValueError("HF archive repo 尚未設定")
        if not token:
            raise ValueError("HF_TOKEN 尚未設定")
        from huggingface_hub import HfApi
        self.repo_id, self.token = repo_id, token
        self.api = HfApi(token=token)
        cache_root = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".cache") / "audiobook-generator"
        cache_root.mkdir(parents=True, exist_ok=True)
        self.cache_path = cache_root / f"hf-catalog-{hashlib.sha256(repo_id.encode()).hexdigest()[:16]}.json"
        self._network_slots = threading.BoundedSemaphore(16)

    def _json(self, path: str, revision: str) -> dict[str, Any]:
        from huggingface_hub import hf_hub_download
        try:
            local = hf_hub_download(self.repo_id, path, repo_type="dataset", token=self.token,
                                    revision=revision, local_files_only=True)
        except Exception:
            with self._network_slots:
                local = hf_hub_download(self.repo_id, path, repo_type="dataset", token=self.token, revision=revision)
        return json.loads(Path(local).read_text(encoding="utf-8"))

    def _cached_books(self, max_age=15 * 60) -> list[HfBook] | None:
        try:
            if time.time() - self.cache_path.stat().st_mtime > max_age:
                return None
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if int(data.get("schema_version") or 0) < 2:
                return None
            return [HfBook(item["key"], item["title"], item["root"], item["revision"],
                           [HfPart(**part) for part in item.get("parts") or []], item.get("error", ""))
                    for item in data.get("books") or []]
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save_cache(self, books: list[HfBook]) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema_version": 2, "repo_id": self.repo_id, "books": [asdict(book) for book in books]},
                                        ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.cache_path)

    def list_books(self, force_refresh=False, revision: str | None = None) -> list[HfBook]:
        pinned_revision = bool(revision)
        if not force_refresh and not revision:
            cached = self._cached_books()
            if cached is not None:
                return cached
        if not revision:
            info = self.api.repo_info(self.repo_id, repo_type="dataset")
            revision = str(getattr(info, "sha", None) or "main")
        files = set(self.api.list_repo_files(self.repo_id, repo_type="dataset", revision=revision))
        indexes = sorted(path for path in files if path.startswith("有聲小說/") and path.endswith("/part_index.json"))
        def read_book(index_path):
            root = index_path.rsplit("/", 1)[0]
            key = root.split("/", 1)[1]
            try:
                index = self._json(index_path, revision)
                raw_parts = sorted(index.get("parts") or [], key=lambda x: int(x.get("part_number") or 0))
                def read_part(pair):
                    expected, raw = pair
                    number = int(raw.get("part_number") or 0)
                    if number != expected:
                        raise ValueError(f"Part 編號不連續：預期 {expected}，實際 {number}")
                    video = raw.get("video") or {}
                    video_path = str(video.get("path") or "")
                    media_path = f"{root}/{Path(video_path).parent.name}/media_info.json"
                    if not video_path or video_path not in files:
                        raise ValueError(f"Part {number} 缺少 MP4")
                    if media_path not in files:
                        raise ValueError(f"Part {number} 缺少 media_info.json")
                    merge_manifest_path = f"{root}/{Path(video_path).parent.name}/merge_manifest.json"
                    if merge_manifest_path not in files:
                        raise ValueError(f"Part {number} 缺少 merge_manifest.json")
                    seconds = duration_seconds(self._json(media_path, revision))
                    if seconds <= 0:
                        raise ValueError(f"Part {number} 沒有有效時長")
                    merge_manifest = self._json(merge_manifest_path, revision)
                    timeline = list(((merge_manifest.get("part") or {}).get("chapter_timeline") or []))
                    if not timeline:
                        raise ValueError(f"Part {number} 缺少章節時間軸")
                    return HfPart(number, int(raw.get("start_chapter") or 0), int(raw.get("end_chapter") or 0),
                                  video_path, int(video.get("bytes") or 0), str(video.get("sha256") or ""), seconds,
                                  timeline)
                with ThreadPoolExecutor(max_workers=min(12, max(1, len(raw_parts)))) as pool:
                    parts = list(pool.map(read_part, enumerate(raw_parts, 1)))
                for position, part in enumerate(parts):
                    if part.start_chapter <= 0 or part.end_chapter < part.start_chapter:
                        raise ValueError(f"Part {part.number} 章節範圍無效")
                    if position and part.start_chapter != parts[position - 1].end_chapter + 1:
                        raise ValueError(f"Part {parts[position - 1].number} 與 Part {part.number} 章節不連續")
                return HfBook(key, str(index.get("book_title") or key), root, revision, parts)
            except Exception as exc:
                return HfBook(key, key, root, revision, error=str(exc))
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(indexes)))) as pool:
            books = list(pool.map(read_book, indexes))
        books = sorted(books, key=lambda book: book.title)
        if not force_refresh and not pinned_revision:
            self._save_cache(books)
        return books
