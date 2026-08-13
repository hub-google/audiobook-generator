"""Durable per-chapter, per-stage progress ledger for matrix workers.

Output files remain the source of truth.  The JSON ledger exists to make
resume decisions, failures, attempts, and GitHub summaries explicit.
"""

from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone

try:
    from .artifact_validation import ArtifactValidationError, validate_stage
except ImportError:
    from artifact_validation import ArtifactValidationError, validate_stage


STAGES = ("crawler", "cleaner", "tts", "subtitle", "image", "video")
STAGE_LABELS = {
    "crawler": "來源／抓文", "cleaner": "清理切段", "tts": "語音",
    "subtitle": "字幕", "image": "章節圖", "video": "影片",
}
STAGE_INPUTS = {
    "crawler": (), "cleaner": ("crawler",), "tts": ("cleaner",),
    "subtitle": ("cleaner", "tts"), "image": ("crawler",),
    "video": ("tts", "subtitle", "image"),
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class PipelineCheckpoint:
    def __init__(self, workspace_dir, book_title, worker_id, chapters):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.book_title = book_title
        self.worker_id = int(worker_id)
        self.chapter_numbers = [int(chapter) for chapter in chapters]
        checkpoint_dir = os.path.join(self.workspace_dir, "Checkpoints")
        os.makedirs(checkpoint_dir, exist_ok=True)
        self.path = os.path.join(checkpoint_dir, f"worker-{self.worker_id}.json")
        self.data = self._load()
        self.reconcile()

    def _empty(self):
        return {
            "schema_version": 2,
            "book_title": self.book_title,
            "worker_id": self.worker_id,
            "chapters": {},
            "worker_stages": {},
            "updated_at": _utc_now(),
        }

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, encoding="utf-8") as checkpoint_file:
                data = json.load(checkpoint_file)
            if not isinstance(data.get("chapters"), dict):
                raise ValueError("chapters must be an object")
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            # A damaged ledger must never block recovery from real output files.
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return self._empty()

    def output_path(self, chapter, stage):
        chapter = int(chapter)
        suffixes = {
            "crawler": ("RawText", f"{self.book_title}_chapter_{chapter}_raw.txt", 10),
            "cleaner": ("CleanText", f"{self.book_title}_chapter_{chapter}_clean.txt", 10),
            "tts": ("Audio", f"{self.book_title}_chapter_{chapter}.wav", 100),
            "subtitle": ("Subtitles", f"{self.book_title}_chapter_{chapter}.srt", 10),
            "image": ("Images", f"{self.book_title}_chapter_{chapter}.jpg", 100),
            "video": ("Video", f"{self.book_title}_chapter_{chapter}.mp4", 1000),
        }
        directory, filename, _ = suffixes[stage]
        return os.path.join(self.workspace_dir, directory, filename)

    def output_exists(self, chapter, stage):
        try:
            self.validate_output(chapter, stage)
            return True
        except (ArtifactValidationError, OSError, ValueError):
            return False

    def validate_output(self, chapter, stage):
        return validate_stage(
            stage, self.output_path(chapter, stage), workspace_dir=self.workspace_dir,
            chapter=int(chapter), book_title=self.book_title,
        )

    def _input_signature(self, chapter, stage):
        values = []
        for upstream in STAGE_INPUTS[stage]:
            upstream_record = self._stage_record(chapter, upstream)
            digest = (upstream_record.get("validation") or {}).get("sha256")
            if not digest:
                try:
                    digest = self.validate_output(chapter, upstream).get("sha256")
                except (ArtifactValidationError, OSError, ValueError):
                    digest = "missing"
            values.append(f"{upstream}:{digest}")
        return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()

    def _stage_record(self, chapter, stage):
        chapter_record = self.data["chapters"].setdefault(
            str(int(chapter)), {"overall_status": "pending", "stages": {}}
        )
        return chapter_record["stages"].setdefault(
            stage, {"status": "pending", "attempts": 0}
        )

    def reconcile(self):
        """Rebuild completion from files and invalidate downstream false success."""
        for chapter in self.chapter_numbers:
            chapter_record = self.data["chapters"].setdefault(
                str(int(chapter)), {"overall_status": "pending", "stages": {}}
            )
            if (chapter_record.get("source") or {}).get("status") == "source_missing":
                for stage in STAGES:
                    record = self._stage_record(chapter, stage)
                    record["status"] = "skipped"
                    record["reason"] = "source_missing"
                self._refresh_chapter(chapter)
                continue
            upstream_complete = True
            for stage in STAGES:
                record = self._stage_record(chapter, stage)
                try:
                    validation = self.validate_output(chapter, stage)
                    valid = True
                    validation_error = None
                except (ArtifactValidationError, OSError, ValueError) as error:
                    validation = None
                    valid = False
                    validation_error = str(error)
                input_signature = self._input_signature(chapter, stage)
                recorded_signature = record.get("input_signature")
                stale = bool(recorded_signature and recorded_signature != input_signature)
                if valid and upstream_complete and not stale:
                    record.update({
                        "status": "completed",
                        "output": os.path.relpath(
                            self.output_path(chapter, stage), self.workspace_dir
                        ).replace("\\", "/"),
                        "validation": validation,
                        "input_signature": input_signature,
                    })
                    record.pop("error", None)
                    record.pop("error_type", None)
                else:
                    upstream_complete = False
                    if stale:
                        record["validation_error"] = "upstream artifact changed; output must be regenerated"
                    elif validation_error:
                        record["validation_error"] = validation_error[:1000]
                    if record.get("status") in ("completed", "running"):
                        record["status"] = "pending"
            self._refresh_chapter(chapter)
        for record in self.data.setdefault("worker_stages", {}).values():
            if record.get("status") == "completed":
                outputs = record.get("outputs") or []
                if not outputs or not all(os.path.exists(path) for path in outputs):
                    record["status"] = "pending"
        self.save()

    def _refresh_chapter(self, chapter):
        chapter_record = self.data["chapters"][str(int(chapter))]
        if (chapter_record.get("source") or {}).get("status") == "source_missing":
            chapter_record["overall_status"] = "source_missing"
            chapter_record["resume_from"] = None
            return
        statuses = [chapter_record["stages"][stage]["status"] for stage in STAGES]
        if all(status == "completed" for status in statuses):
            chapter_record["overall_status"] = "completed"
            chapter_record["resume_from"] = None
        elif "failed" in statuses:
            chapter_record["overall_status"] = "failed"
            chapter_record["resume_from"] = STAGES[statuses.index("failed")]
        else:
            chapter_record["overall_status"] = "pending"
            chapter_record["resume_from"] = STAGES[next(
                index for index, status in enumerate(statuses) if status != "completed"
            )]

    def mark_running(self, chapter, stage):
        record = self._stage_record(chapter, stage)
        record["status"] = "running"
        record["attempts"] = int(record.get("attempts", 0)) + 1
        record["started_at"] = _utc_now()
        record.pop("error", None)
        record.pop("error_type", None)
        self._refresh_chapter(chapter)
        self.save()

    def prepare_for_run(self, chapter, stages):
        """Remove invalid/stale formal outputs so legacy generators cannot skip them."""
        for stage in stages:
            if self.is_completed(chapter, stage):
                continue
            path = self.output_path(chapter, stage)
            if os.path.exists(path):
                os.remove(path)
            for suffix in (".tmp", ".tmp.wav", ".partial", ".partial.mp4"):
                partial = path + suffix
                if os.path.exists(partial):
                    os.remove(partial)

    def mark_completed(self, chapter, stage):
        try:
            validation = self.validate_output(chapter, stage)
        except (ArtifactValidationError, OSError, ValueError) as error:
            raise RuntimeError(f"chapter {chapter} stage {stage} validation failed: {error}") from error
        record = self._stage_record(chapter, stage)
        record.update({
            "status": "completed",
            "completed_at": _utc_now(),
            "output": os.path.relpath(
                self.output_path(chapter, stage), self.workspace_dir
            ).replace("\\", "/"),
            "validation": validation,
            "input_signature": self._input_signature(chapter, stage),
        })
        record.pop("error", None)
        record.pop("error_type", None)
        record.pop("validation_error", None)
        self._refresh_chapter(chapter)
        self.save()

    def mark_failed(self, chapter, stage, error):
        record = self._stage_record(chapter, stage)
        record.update({
            "status": "failed",
            "failed_at": _utc_now(),
            "error_type": type(error).__name__,
            "error": str(error)[:2000],
        })
        self._refresh_chapter(chapter)
        self.save()

    def mark_source_missing(self, chapter, error, evidence=None):
        """Accept a confirmed origin omission as a terminal warning, not a failure."""
        chapter_record = self.data["chapters"].setdefault(
            str(int(chapter)), {"overall_status": "pending", "stages": {}}
        )
        chapter_record["source"] = {
            "status": "source_missing",
            "reason": str(error)[:2000],
            "confirmed_at": _utc_now(),
            "evidence": evidence or {},
        }
        for stage in STAGES:
            record = self._stage_record(chapter, stage)
            record.update({"status": "skipped", "reason": "source_missing"})
            record.pop("error", None)
            record.pop("validation_error", None)
        self._refresh_chapter(chapter)
        self.save()

    def is_completed(self, chapter, stage):
        if not self.output_exists(chapter, stage):
            return False
        record = self._stage_record(chapter, stage)
        recorded = record.get("input_signature")
        return not recorded or recorded == self._input_signature(chapter, stage)

    def mark_worker_stage_running(self, stage):
        record = self.data.setdefault("worker_stages", {}).setdefault(
            stage, {"status": "pending", "attempts": 0}
        )
        record.update({
            "status": "running",
            "attempts": int(record.get("attempts", 0)) + 1,
            "started_at": _utc_now(),
        })
        record.pop("error", None)
        record.pop("error_type", None)
        self.save()

    def mark_worker_stage_completed(self, stage, outputs):
        if isinstance(outputs, (str, os.PathLike)):
            outputs = [outputs]
        else:
            outputs = list(outputs)
        invalid = [path for path in outputs if not isinstance(path, (str, os.PathLike))]
        if invalid:
            raise TypeError(
                f"worker stage {stage} outputs must be file paths; "
                f"got {type(invalid[0]).__name__}"
            )
        outputs = [os.path.abspath(path) for path in outputs]
        if not outputs or not all(os.path.exists(path) for path in outputs):
            raise RuntimeError(f"worker stage {stage} has missing output files")
        record = self.data.setdefault("worker_stages", {}).setdefault(stage, {})
        record.update({"status": "completed", "completed_at": _utc_now(), "outputs": outputs})
        record.pop("error", None)
        record.pop("error_type", None)
        self.save()

    def mark_worker_stage_failed(self, stage, error):
        record = self.data.setdefault("worker_stages", {}).setdefault(stage, {})
        record.update({
            "status": "failed",
            "failed_at": _utc_now(),
            "error_type": type(error).__name__,
            "error": str(error)[:2000],
        })
        self.save()

    def incomplete_chapters(self):
        # NOTE: callers who need fresh data should call reconcile() first.
        # Removed the automatic reconcile() here because __init__ already
        # calls it, and the triple-reconcile (init + incomplete + summary)
        # caused Worker Job Summary to hang for 10+ minutes on 62-chapter
        # workers due to repeated ffprobe + sha256 on every artifact.
        return [chapter for chapter in self.chapter_numbers if (
            self.data["chapters"].get(str(chapter), {}).get("overall_status") != "source_missing"
            and any(self._stage_record(chapter, stage).get("status") != "completed" for stage in STAGES)
        )]

    def source_missing_chapters(self):
        return [chapter for chapter in self.chapter_numbers if (
            self.data["chapters"].get(str(chapter), {}).get("overall_status") == "source_missing"
        )]

    def save(self):
        self.data["updated_at"] = _utc_now()
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as checkpoint_file:
            json.dump(self.data, checkpoint_file, ensure_ascii=False, indent=2)
            checkpoint_file.flush()
            os.fsync(checkpoint_file.fileno())
        os.replace(temp_path, self.path)

    def markdown_summary(self):
        # NOTE: reconcile() is NOT called here; the caller or __init__
        # should have already called it.  See incomplete_chapters() note.
        total = len(self.chapter_numbers)
        missing_chapters = self.source_missing_chapters()
        completed_chapters = total - len(self.incomplete_chapters()) - len(missing_chapters)
        worker_failures = [
            (stage, record) for stage, record in self.data.get("worker_stages", {}).items()
            if record.get("status") != "completed"
        ]
        all_complete = completed_chapters + len(missing_chapters) == total and not worker_failures
        lines = [
            f"## Worker {self.worker_id} Pipeline Status",
            "",
            f"- Overall: **{'COMPLETED' if all_complete else 'FAILED'}**",
            f"- Complete chapters: **{completed_chapters} / {total}**",
            f"- Origin website missing: **{len(missing_chapters)}**",
            "",
            "| 章節 | 來源／抓文 | 清理切段 | 語音 | 字幕 | 章節圖 | 影片 | 最終驗收 |",
            "|---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
        ]
        icons = {"completed": "✅", "running": "🔄", "failed": "❌", "pending": "⏳"}
        for chapter in self.chapter_numbers:
            chapter_record = self.data["chapters"][str(chapter)]
            cells = [icons.get(chapter_record["stages"][stage].get("status"), "⏳") for stage in STAGES]
            if chapter_record.get("overall_status") == "source_missing":
                final = "⚠️ 來源缺章"
            else:
                final = "✅ 通過" if chapter_record.get("overall_status") == "completed" else "❌ 未通過"
            lines.append(f"| 第 {chapter} 章 | {' | '.join(cells)} | {final} |")
        if missing_chapters:
            lines.extend([
                "", "### Confirmed origin omissions", "",
                "These chapter URLs repeatedly returned HTTP 200 but no article content.", "",
                "- Chapters: " + ", ".join(str(chapter) for chapter in missing_chapters),
            ])
        failures = []
        for chapter in self.chapter_numbers:
            chapter_record = self.data["chapters"][str(chapter)]
            if chapter_record["overall_status"] in ("completed", "source_missing"):
                continue
            resume_from = chapter_record.get("resume_from") or "unknown"
            stage_record = chapter_record["stages"].get(resume_from, {})
            reason = stage_record.get("error") or stage_record.get("validation_error") or "required output file is missing or invalid"
            failures.append((chapter, resume_from, reason.replace("\n", " ")))
        if failures:
            lines.extend(["", "### Resume points", "", "| Chapter | Resume from | Reason |", "|---:|---|---|"])
            for chapter, stage, reason in failures:
                lines.append(f"| {chapter} | {stage} | {reason} |")
        if worker_failures:
            lines.extend(["", "### Worker-level failures", ""])
            for stage, record in worker_failures:
                reason = record.get("error") or "required output file is missing"
                lines.append(f"- **{stage}**: {reason}")
        return "\n".join(lines) + "\n"
