"""Persistent Hugging Face backup for merged audiobook Parts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download


def _now():
    return datetime.now(timezone.utc).isoformat()


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip())
    return re.sub(r"\s+", " ", value).strip(" ._") or "未命名"


def sha256_file(path, chunk_size=8 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def media_info(path):
    command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        return {"probe_error": result.stderr[-2000:]}
    return json.loads(result.stdout)


class HuggingFaceArchiver:
    def __init__(self, repo_id, token, state_file, project="有聲小說", private=True):
        if not repo_id:
            raise ValueError("HF_ARCHIVE_REPO is required")
        self.repo_id = repo_id
        self.project = safe_name(project)
        self.api = HfApi(token=token)
        self.api.create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)
        self.state_file = Path(state_file)
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                remote_state = hf_hub_download(
                    repo_id, "_system/archive_state.json", repo_type="dataset", token=token,
                )
                self.state = json.loads(Path(remote_state).read_text(encoding="utf-8"))
            except Exception:
                self.state = {"schema_version": 1, "repo_id": repo_id, "books": {}}

    def _save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.state_file)
        self.api.upload_file(
            path_or_fileobj=str(self.state_file), path_in_repo="_system/archive_state.json",
            repo_id=self.repo_id, repo_type="dataset", commit_message="Update archive checkpoint",
        )

    def _book_root(self, book_title):
        return f"{self.project}/{safe_name(book_title)}"

    def _part_root(self, book_title, part_num, start_chap, end_chap):
        folder = f"{self.project}_{safe_name(book_title)}_第{int(part_num):02d}部_第{int(start_chap):04d}章-第{int(end_chap):04d}章"
        return f"{self._book_root(book_title)}/{folder}"

    def _upload(self, local_path, remote_path, message):
        self.api.upload_file(
            path_or_fileobj=str(local_path), path_in_repo=remote_path,
            repo_id=self.repo_id, repo_type="dataset", commit_message=message,
        )

    def _upload_json(self, value, remote_path, message):
        payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.api.upload_file(
            path_or_fileobj=payload, path_in_repo=remote_path,
            repo_id=self.repo_id, repo_type="dataset", commit_message=message,
        )

    def archive_part(self, *, book_title, part_num, start_chap, end_chap, chapters,
                     video_path, subtitle_path, master_cover_path, source_config_path=None,
                     run_id="", task_id="", source_missing_chapters=None):
        video = Path(video_path)
        subtitle = Path(subtitle_path)
        cover = Path(master_cover_path)
        for required in (video, subtitle, cover):
            if not required.is_file() or required.stat().st_size <= 0:
                raise RuntimeError(f"HF archive input is missing or empty: {required}")

        root = self._book_root(book_title)
        part_root = self._part_root(book_title, part_num, start_chap, end_chap)
        video_remote = f"{part_root}/{video.name}"
        subtitle_remote = f"{part_root}/{subtitle.name}"
        fingerprint = {
            "video": {"path": video_remote, "bytes": video.stat().st_size, "sha256": sha256_file(video)},
            "subtitle": {"path": subtitle_remote, "bytes": subtitle.stat().st_size, "sha256": sha256_file(subtitle)},
            "master_cover": {"path": f"{root}/master_cover.jpg", "bytes": cover.stat().st_size, "sha256": sha256_file(cover)},
        }
        book = self.state["books"].setdefault(book_title, {"parts": {}, "root": root})
        previous = book["parts"].get(str(int(part_num)), {})
        if previous.get("status") in {"complete", "uploaded_pending_youtube_metadata"} and previous.get("files") == fingerprint:
            return previous

        manifest = {
            "project": self.project, "book_title": book_title, "part_number": int(part_num),
            "start_chapter": int(start_chap), "end_chapter": int(end_chap),
            "chapters": [int(value) for value in chapters],
            "source_missing_chapters": [int(value) for value in (source_missing_chapters or [])],
            "source_run_id": str(run_id or ""), "queue_task_id": str(task_id or ""),
            "files": fingerprint, "archived_at": _now(), "status": "uploaded_pending_youtube_metadata",
        }
        info = media_info(video)
        message = f"Archive {book_title} Part {int(part_num):02d}"
        self._upload(video, video_remote, message)
        self._upload(subtitle, subtitle_remote, message)
        self._upload(cover, f"{root}/master_cover.jpg", f"Archive {book_title} master cover")
        if source_config_path and Path(source_config_path).is_file():
            self._upload(source_config_path, f"{root}/source_config.yaml", f"Archive {book_title} source config")
        self._upload_json(manifest, f"{part_root}/part_manifest.json", message)
        self._upload_json(info, f"{part_root}/media_info.json", message)
        record = {"status": "uploaded_pending_youtube_metadata", "root": part_root, "files": fingerprint, "manifest": manifest}
        book["parts"][str(int(part_num))] = record
        self._save()
        return record

    def register_preuploaded_part(self, *, book_title, part_num, start_chap, end_chap,
                                  chapters, video_path, subtitle_path, master_cover_path,
                                  source_config_path=None, run_id="", task_id="",
                                  source_missing_chapters=None):
        """Adopt media uploaded independently by a merge worker.

        Matrix workers write to the final unique Part paths, so the ordered
        publisher only verifies and records them; it never uploads large media.
        """
        video, subtitle, cover = Path(video_path), Path(subtitle_path), Path(master_cover_path)
        part_root = self._part_root(book_title, part_num, start_chap, end_chap)
        root = self._book_root(book_title)
        video_remote, subtitle_remote = f"{part_root}/{video.name}", f"{part_root}/{subtitle.name}"
        remote = set(self.api.list_repo_files(self.repo_id, repo_type="dataset"))
        missing = {video_remote, subtitle_remote} - remote
        if missing:
            raise RuntimeError(f"merge worker HF upload is missing: {sorted(missing)}")
        fingerprint = {
            "video": {"path": video_remote, "bytes": video.stat().st_size, "sha256": sha256_file(video)},
            "subtitle": {"path": subtitle_remote, "bytes": subtitle.stat().st_size, "sha256": sha256_file(subtitle)},
            "master_cover": {"path": f"{root}/master_cover.jpg", "bytes": cover.stat().st_size, "sha256": sha256_file(cover)},
        }
        manifest = {
            "project": self.project, "book_title": book_title, "part_number": int(part_num),
            "start_chapter": int(start_chap), "end_chapter": int(end_chap),
            "chapters": [int(value) for value in chapters],
            "source_missing_chapters": [int(value) for value in (source_missing_chapters or [])],
            "source_run_id": str(run_id or ""), "queue_task_id": str(task_id or ""),
            "files": fingerprint, "archived_at": _now(), "status": "uploaded_pending_youtube_metadata",
        }
        self._upload(cover, f"{root}/master_cover.jpg", f"Archive {book_title} master cover")
        if source_config_path and Path(source_config_path).is_file():
            self._upload(source_config_path, f"{root}/source_config.yaml", f"Archive {book_title} source config")
        self._upload_json(manifest, f"{part_root}/part_manifest.json", f"Register {book_title} Part {int(part_num):02d}")
        self._upload_json(media_info(video), f"{part_root}/media_info.json", f"Register {book_title} Part {int(part_num):02d}")
        record = {"status": "uploaded_pending_youtube_metadata", "root": part_root, "files": fingerprint, "manifest": manifest}
        book = self.state["books"].setdefault(book_title, {"parts": {}, "root": root})
        book["parts"][str(int(part_num))] = record
        self._save()
        return record

    def finalize_part(self, *, book_title, part_num, youtube_video_id, playlist_id,
                      title, description, privacy, playlist_position):
        book = self.state["books"][book_title]
        record = book["parts"][str(int(part_num))]
        metadata = {
            "title": title, "description": description,
            "youtube_video_id": youtube_video_id, "youtube_playlist_id": playlist_id,
            "playlist_position": int(playlist_position), "privacy": privacy,
            "caption_language": "zh-TW", "finalized_at": _now(),
        }
        self._upload_json(metadata, f"{record['root']}/youtube_metadata.json", f"Finalize {book_title} Part {int(part_num):02d}")
        record["youtube"] = metadata
        record["status"] = "complete"
        record["manifest"]["status"] = "complete"
        record["manifest"]["youtube"] = metadata
        self._upload_json(record["manifest"], f"{record['root']}/part_manifest.json", f"Complete {book_title} Part {int(part_num):02d}")
        self._write_book_indexes(book_title)
        self._save()
        return record

    def _write_book_indexes(self, book_title):
        book = self.state["books"][book_title]
        parts = [book["parts"][key] for key in sorted(book["parts"], key=lambda value: int(value))]
        index = {
            "project": self.project, "book_title": book_title,
            "parts": [{
                "part_number": item["manifest"]["part_number"],
                "start_chapter": item["manifest"]["start_chapter"],
                "end_chapter": item["manifest"]["end_chapter"],
                "video": item["files"]["video"], "subtitle": item["files"]["subtitle"],
                "status": item["status"],
            } for item in parts],
            "updated_at": _now(),
        }
        complete = sum(item["status"] == "complete" for item in parts)
        manifest = {
            "project": self.project, "book_title": book_title,
            "master_cover": parts[0]["files"]["master_cover"] if parts else None,
            "part_count": len(parts), "completed_parts": complete,
            "archive_status": "complete" if parts and complete == len(parts) else "running",
            "updated_at": _now(),
        }
        self._upload_json(index, f"{book['root']}/part_index.json", f"Update {book_title} Part index")
        self._upload_json(manifest, f"{book['root']}/book_manifest.json", f"Update {book_title} manifest")

    def completed_parts(self, book_title):
        book = self.state.get("books", {}).get(book_title, {})
        return {int(number) for number, item in book.get("parts", {}).items() if item.get("status") == "complete"}

    def verify_book(self, book_title, expected_parts):
        book = self.state.get("books", {}).get(book_title, {})
        parts = book.get("parts", {})
        missing = [number for number in range(1, int(expected_parts) + 1) if parts.get(str(number), {}).get("status") != "complete"]
        remote = set(self.api.list_repo_files(self.repo_id, repo_type="dataset"))
        required = {f"{book.get('root')}/master_cover.jpg", f"{book.get('root')}/book_manifest.json", f"{book.get('root')}/part_index.json"}
        for item in parts.values():
            if item.get("status") == "complete":
                required.update({item["files"]["video"]["path"], item["files"]["subtitle"]["path"], f"{item['root']}/part_manifest.json", f"{item['root']}/youtube_metadata.json", f"{item['root']}/media_info.json"})
        absent = sorted(required - remote)
        if missing or absent:
            raise RuntimeError(f"HF archive verification failed; incomplete Parts={missing}, missing files={absent[:10]}")
        return {"repo_id": self.repo_id, "book_root": book.get("root"), "parts": int(expected_parts), "verified": True}
