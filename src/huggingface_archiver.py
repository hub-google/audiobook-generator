"""Persistent Hugging Face backup for merged audiobook Parts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download


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
    def __init__(self, repo_id, token, state_file, project="有聲小說", private=False):
        if not repo_id:
            raise ValueError("HF_ARCHIVE_REPO is required")
        if private is None:
            private = os.environ.get("HF_DATASET_PRIVATE", "false").lower() == "true"
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

    @staticmethod
    def _json_operation(value, remote_path):
        payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return CommitOperationAdd(path_in_repo=remote_path, path_or_fileobj=io.BytesIO(payload))

    def archive_part(self, *, book_title, part_num, start_chap, end_chap, chapters,
                     video_path, subtitle_path, master_cover_path, source_config_path=None,
                     run_id="", task_id="", source_missing_chapters=None):
        video = Path(video_path)
        for required in (video,):
            if not required.is_file() or required.stat().st_size <= 0:
                raise RuntimeError(f"HF archive input is missing or empty: {required}")

        root = self._book_root(book_title)
        part_root = self._part_root(book_title, part_num, start_chap, end_chap)
        video_remote = f"{part_root}/{video.name}"
        fingerprint = {
            "video": {"path": video_remote, "bytes": video.stat().st_size, "sha256": sha256_file(video)},
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
            "files": fingerprint, "archived_at": _now(), "status": "complete",
        }
        subtitle = Path(subtitle_path)
        if not subtitle.is_file() or subtitle.stat().st_size <= 0:
            raise RuntimeError(f"HF archive input is missing or empty: {subtitle}")
        subtitle_remote = f"{part_root}/{subtitle.name}"
        fingerprint["subtitle"] = {"path": subtitle_remote, "bytes": subtitle.stat().st_size, "sha256": sha256_file(subtitle)}
        manifest["files"] = fingerprint
        manifest["status"] = "uploaded_pending_youtube_metadata"
        operations = [
            CommitOperationAdd(path_in_repo=video_remote, path_or_fileobj=str(video)),
            CommitOperationAdd(path_in_repo=subtitle_remote, path_or_fileobj=str(subtitle)),
            self._json_operation(manifest, f"{part_root}/part_manifest.json"),
            self._json_operation(media_info(video), f"{part_root}/media_info.json"),
        ]
        merge_manifest = {"schema_version": 1, "status": "merge_complete", "source_run_id": str(run_id or ""), "book_title": book_title, "part": manifest, "files": fingerprint}
        operations.append(self._json_operation(merge_manifest, f"{part_root}/merge_manifest.json"))
        self.api.create_commit(repo_id=self.repo_id, repo_type="dataset", operations=operations,
                               commit_message=f"Archive {book_title} Part {int(part_num):02d}")
        record = {"status": "uploaded_pending_youtube_metadata", "root": part_root, "files": fingerprint, "manifest": manifest}
        book["parts"][str(int(part_num))] = record
        book["master_cover_path"] = str(master_cover_path)
        if source_config_path: book["source_config_path"] = str(source_config_path)
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
        video, subtitle = Path(video_path), Path(subtitle_path)
        part_root = self._part_root(book_title, part_num, start_chap, end_chap)
        root = self._book_root(book_title)
        video_remote, subtitle_remote = f"{part_root}/{video.name}", f"{part_root}/{subtitle.name}"
        remote = set(self.api.list_repo_files(self.repo_id, repo_type="dataset"))
        required = {video_remote, subtitle_remote, f"{part_root}/merge_manifest.json", f"{part_root}/part_manifest.json", f"{part_root}/media_info.json"}
        missing = required - remote
        if missing:
            raise RuntimeError(f"merge worker HF upload is missing: {sorted(missing)}")
        fingerprint = {
            "video": {"path": video_remote, "bytes": video.stat().st_size, "sha256": sha256_file(video)},
            "subtitle": {"path": subtitle_remote, "bytes": subtitle.stat().st_size, "sha256": sha256_file(subtitle)},
        }
        manifest = {
            "project": self.project, "book_title": book_title, "part_number": int(part_num),
            "start_chapter": int(start_chap), "end_chapter": int(end_chap),
            "chapters": [int(value) for value in chapters],
            "source_missing_chapters": [int(value) for value in (source_missing_chapters or [])],
            "source_run_id": str(run_id or ""), "queue_task_id": str(task_id or ""),
            "files": fingerprint, "archived_at": _now(), "status": "complete",
        }
        record = {"status": "complete", "root": part_root, "files": fingerprint, "manifest": manifest}
        book = self.state["books"].setdefault(book_title, {"parts": {}, "root": root})
        book["parts"][str(int(part_num))] = record
        book["master_cover_path"] = str(master_cover_path)
        if source_config_path: book["source_config_path"] = str(source_config_path)
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
        record["youtube"] = metadata
        record["status"] = "complete"
        record["manifest"]["status"] = "complete"
        record["manifest"]["youtube"] = metadata
        self._save()
        return record

    def _write_book_indexes(self, book_title):
        """Kept for API compatibility; final indexes are batch-written by verify_book."""
        self._save()

    def completed_parts(self, book_title):
        book = self.state.get("books", {}).get(book_title, {})
        return {int(number) for number, item in book.get("parts", {}).items() if item.get("status") == "complete"}

    def verify_book(self, book_title, expected_parts):
        book = self.state.get("books", {}).get(book_title, {})
        parts = book.get("parts", {})
        missing = [number for number in range(1, int(expected_parts) + 1) if parts.get(str(number), {}).get("status") != "complete"]
        remote = set(self.api.list_repo_files(self.repo_id, repo_type="dataset"))
        required = set()
        for item in parts.values():
            if item.get("status") == "complete":
                required.update({item["files"]["video"]["path"], item["files"]["subtitle"]["path"], f"{item['root']}/merge_manifest.json", f"{item['root']}/media_info.json"})
        absent = sorted(required - remote)
        if missing or absent:
            raise RuntimeError(f"HF archive verification failed; incomplete Parts={missing}, missing files={absent[:10]}")
        part_records = [parts[key] for key in sorted(parts, key=lambda value: int(value))]
        index = {"project": self.project, "book_title": book_title, "parts": [{"part_number": item["manifest"]["part_number"], "start_chapter": item["manifest"]["start_chapter"], "end_chapter": item["manifest"]["end_chapter"], "video": item["files"]["video"], "subtitle": item["files"]["subtitle"], "status": item["status"]} for item in part_records], "updated_at": _now()}
        book_manifest = {"project": self.project, "book_title": book_title, "master_cover": f"{book.get('root')}/master_cover.jpg", "part_count": len(part_records), "completed_parts": len(part_records), "archive_status": "complete", "updated_at": _now()}
        operations = []
        for item in part_records:
            operations.append(self._json_operation(item["youtube"], f"{item['root']}/youtube_metadata.json"))
            operations.append(self._json_operation(item["manifest"], f"{item['root']}/part_manifest.json"))
        operations.extend([
            self._json_operation(index, f"{book['root']}/part_index.json"),
            self._json_operation(book_manifest, f"{book['root']}/book_manifest.json"),
            self._json_operation(self.state, "_system/archive_state.json"),
        ])
        cover = Path(book.get("master_cover_path") or "")
        if cover.is_file(): operations.append(CommitOperationAdd(path_in_repo=f"{book['root']}/master_cover.jpg", path_or_fileobj=str(cover)))
        config = Path(book.get("source_config_path") or "")
        if config.is_file(): operations.append(CommitOperationAdd(path_in_repo=f"{book['root']}/source_config.yaml", path_or_fileobj=str(config)))
        self.api.create_commit(repo_id=self.repo_id, repo_type="dataset", operations=operations,
                               commit_message=f"Finalize {book_title} archive metadata")
        return {"repo_id": self.repo_id, "book_root": book.get("root"), "parts": int(expected_parts), "verified": True}
