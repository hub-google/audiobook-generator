"""Locked Part plan and durable per-Part publication ledger."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone


PART_STEPS = (
    "prepare_chapters", "generate_subtitle", "merge_video", "validate_video",
    "generate_metadata_cover", "archive_hf", "upload_video", "upload_thumbnail",
    "upload_caption", "add_playlist", "publish", "final_validation",
)
PART_LABELS = {
    "prepare_chapters": "準備章節", "generate_subtitle": "產生字幕",
    "merge_video": "合併影片", "validate_video": "驗證影片",
    "generate_metadata_cover": "資料／封面", "upload_video": "上傳影片",
    "archive_hf": "HF 完整備份",
    "upload_thumbnail": "上傳封面", "upload_caption": "上傳字幕",
    "add_playlist": "播放清單", "publish": "最終發布",
    "final_validation": "最終驗收",
}
GLOBAL_STEPS = ("download_artifacts", "validate_inventory", "probe_durations", "lock_plan", "playlist", "final_book_validation")
GLOBAL_LABELS = {
    "download_artifacts": "下載全部 Worker Artifacts", "validate_inventory": "驗收章節完整性",
    "probe_durations": "讀取章節影片時長", "lock_plan": "建立並鎖定 Part 規劃",
    "playlist": "建立／取得播放清單", "final_book_validation": "全書最終驗收",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def normalized_plan(plan):
    return [{
        "part_num": int(part["part_num"]),
        "start_chap": int(part.get("start_chap", 0)),
        "end_chap": int(part.get("end_chap", 0)),
        "chapters": [int(value) for value in part.get("chapters", [])],
        "source_missing_chapters": [
            int(value) for value in part.get("source_missing_chapters", [])
        ],
        "title": str(part.get("title") or ""),
    } for part in plan]


def plan_fingerprint(plan):
    payload = json.dumps(normalized_plan(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PublicationCheckpoint:
    def __init__(self, state_file):
        self.path = os.path.join(os.path.dirname(os.path.abspath(state_file)), "part_execution.json")
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {"schema_version": 2, "plan_status": "unplanned", "parts": {}, "global_steps": {}}
        self.data.setdefault("global_steps", {})
        self._migrate_legacy()

    def is_locked(self):
        return self.data.get("plan_status") == "locked" and bool(self.data.get("plan"))

    def _migrate_legacy(self):
        """Keep old ledgers resumable while new writes use explicit API acknowledgements."""
        self.data["schema_version"] = 2
        for record in (self.data.get("parts") or {}).values():
            steps = record.get("steps") or {}
            upload = steps.get("upload_video") or {}
            video_id = upload.get("youtube_video_id")
            record.setdefault("upload", {
                "status": upload.get("status", "pending"), "video_id": video_id,
                "completed_at": upload.get("updated_at") or upload.get("completed_at"),
            })
            thumb = steps.get("upload_thumbnail") or {}
            record.setdefault("thumbnail", {"status": thumb.get("status", "pending"),
                                             "completed_at": thumb.get("updated_at")})
            playlist = steps.get("add_playlist") or {}
            record.setdefault("playlist", {"status": playlist.get("status", "pending"),
                                            "playlist_item_id": playlist.get("playlist_item_id"),
                                            "position": playlist.get("position"),
                                            "completed_at": playlist.get("updated_at")})

    def validate_task_identity(self, book_profile_id="", part_plan=None, plan_fingerprint_str="", task_id=""):
        """Verify whether this publication ledger matches the given fingerprint identity."""
        if not self.data.get("plan"):
            return True, ""
        if task_id and self.data.get("task_id"):
            if str(self.data.get("task_id")).strip() != str(task_id).strip():
                return False, f"ledger task_id {self.data.get('task_id')!r} != expected {task_id!r}"
        if book_profile_id and self.data.get("book_profile_id"):
            if str(self.data.get("book_profile_id")).strip() != str(book_profile_id).strip():
                return False, f"ledger book_profile_id {self.data.get('book_profile_id')!r} != expected {book_profile_id!r}"
        expected_fp = plan_fingerprint_str or (plan_fingerprint(part_plan) if part_plan else "")
        if expected_fp:
            ledger_fp = self.data.get("plan_fingerprint") or plan_fingerprint(self.data.get("plan", []))
            if ledger_fp != expected_fp:
                return False, f"ledger plan_fingerprint {ledger_fp!r} != expected {expected_fp!r}"
        return True, ""

    def lock_plan(self, plan, run_id="", book_title="", book_profile_id="", execution_run_id="", task_id=""):
        candidate = normalized_plan(plan)
        fingerprint = plan_fingerprint(candidate)
        existing = self.data.get("plan")
        if existing:
            existing_fp = self.data.get("plan_fingerprint") or plan_fingerprint(existing)
            if existing_fp != fingerprint:
                raise RuntimeError(
                    "locked Part plan differs from current chapter inventory; refusing to repartition"
                )
            if book_profile_id and self.data.get("book_profile_id") and str(self.data.get("book_profile_id")).strip() != str(book_profile_id).strip():
                raise RuntimeError(
                    f"locked Part plan book_profile_id {self.data.get('book_profile_id')!r} differs from current {book_profile_id!r}; refusing foreign checkpoint"
                )
            if not self.data.get("source_run_id") and run_id:
                self.data["source_run_id"] = str(run_id)
            if execution_run_id or run_id:
                self.data["execution_run_id"] = str(execution_run_id or run_id)
            if book_profile_id and not self.data.get("book_profile_id"):
                self.data["book_profile_id"] = str(book_profile_id)
            if book_title and not self.data.get("book_title"):
                self.data["book_title"] = str(book_title)
            if task_id and not self.data.get("task_id"):
                self.data["task_id"] = str(task_id)
            if not self.data.get("plan_fingerprint"):
                self.data["plan_fingerprint"] = existing_fp
            if self.data.get("plan_status") != "locked":
                self.data["plan_status"] = "locked"
            self.save()
            return self.data["plan"]

        self.data.update({
            "source_run_id": str(run_id or ""),
            "execution_run_id": str(execution_run_id or run_id or ""),
            "book_profile_id": str(book_profile_id or ""),
            "book_title": str(book_title or ""),
            "task_id": str(task_id or ""),
            "plan_status": "locked",
            "plan_fingerprint": fingerprint,
            "plan": candidate,
        })
        for part in self.data["plan"]:
            record = self.data.setdefault("parts", {}).setdefault(
                str(part["part_num"]), {"steps": {}, "chapter_range": f"{part['start_chap']}–{part['end_chap']}"}
            )
            for step in PART_STEPS:
                record.setdefault("steps", {}).setdefault(step, {"status": "pending", "attempts": 0})
            # CC upload and private->public transition were retired.  Marking
            # these compatibility columns complete prevents legacy resume from
            # calling Captions API or videos.update.
            for retired in ("upload_caption", "publish"):
                record["steps"][retired].update({"status": "completed", "retired": True})
            record.setdefault("source_part_sha256", "")
            record.setdefault("upload", {"status": "pending", "video_id": None, "completed_at": None})
            record.setdefault("thumbnail", {"status": "pending", "completed_at": None})
            record.setdefault("playlist", {"status": "pending", "playlist_item_id": None,
                                            "position": None, "completed_at": None})
        self.save()
        return self.data["plan"]

    def mark_global(self, step, status, **evidence):
        if step not in GLOBAL_STEPS:
            raise ValueError(step)
        record = self.data["global_steps"].setdefault(step, {"attempts": 0})
        if status == "running":
            record["attempts"] = int(record.get("attempts", 0)) + 1
        record.update({"status": status, "updated_at": _now()})
        record.update({key: value for key, value in evidence.items() if value is not None})
        self.save()

    def _record(self, part_num, step):
        return self.data["parts"][str(int(part_num))]["steps"][step]

    def mark(self, part_num, step, status, **evidence):
        if step not in PART_STEPS:
            raise ValueError(step)
        record = self._record(part_num, step)
        if status == "running":
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["started_at"] = _now()
        record["status"] = status
        record["updated_at"] = _now()
        record.update({key: value for key, value in evidence.items() if value is not None})
        self._refresh(part_num)
        self.save()

    def complete(self, part_num, step, **evidence):
        self.mark(part_num, step, "completed", **evidence)

    def record_upload_ack(self, part_num, video_id, source_part_sha256, **evidence):
        part = self.data["parts"][str(int(part_num))]
        part["source_part_sha256"] = source_part_sha256
        part["upload"] = {"status": "completed", "video_id": video_id,
                          "completed_at": _now(), **evidence}
        self.complete(part_num, "upload_video", youtube_video_id=video_id,
                      source_part_sha256=source_part_sha256, **evidence)

    def record_thumbnail_ack(self, part_num):
        part = self.data["parts"][str(int(part_num))]
        part["thumbnail"] = {"status": "completed", "completed_at": _now()}
        self.complete(part_num, "upload_thumbnail", youtube_video_id=part["upload"].get("video_id"))

    def record_playlist_ack(self, part_num, playlist_item_id, position):
        part = self.data["parts"][str(int(part_num))]
        part["playlist"] = {"status": "completed", "playlist_item_id": playlist_item_id,
                            "position": int(position), "completed_at": _now()}
        self.complete(part_num, "add_playlist", youtube_video_id=part["upload"].get("video_id"),
                      playlist_item_id=playlist_item_id, position=int(position))

    def reset_upload(self, part_num, reason="video_not_found"):
        part = self.data["parts"].get(str(int(part_num)))
        if not part:
            return
        part["upload"] = {"status": "pending", "video_id": None, "completed_at": None}
        part["thumbnail"] = {"status": "pending", "completed_at": None}
        part["playlist"] = {"status": "pending", "playlist_item_id": None, "position": None, "completed_at": None}
        for step in ("upload_video", "upload_thumbnail", "add_playlist", "final_validation"):
            if step in part.get("steps", {}):
                part["steps"][step].update({
                    "status": "pending",
                    "attempts": 0,
                    "youtube_video_id": None,
                    "playlist_item_id": None,
                    "reset_reason": reason,
                    "updated_at": _now(),
                })
        self._refresh(part_num)
        self.save()

    def fail(self, part_num, step, error, paused=False, **evidence):
        self.mark(
            part_num, step, "paused" if paused else "failed",
            error_type=type(error).__name__, error=str(error)[:2000], **evidence,
        )

    def _refresh(self, part_num):
        part = self.data["parts"][str(int(part_num))]
        statuses = [part["steps"][step]["status"] for step in PART_STEPS]
        part["overall_status"] = "completed" if all(value == "completed" for value in statuses) else (
            "failed" if "failed" in statuses else "paused" if "paused" in statuses else "running"
        )
        part["resume_from"] = next((step for step in PART_STEPS if part["steps"][step]["status"] != "completed"), None)

    def save(self):
        self.data["updated_at"] = _now()
        _atomic_json(self.path, self.data)

    def markdown_summary(self):
        plan = self.data.get("plan") or []
        lines = [
            "## 全書準備與 Part 規劃", "",
            f"- Part 規劃：**{'🔒 已鎖定' if self.data.get('plan_status') == 'locked' else '尚未建立'}**",
            f"- 規劃指紋：`{self.data.get('plan_fingerprint', '尚無')}`", "",
            "| 全書準備項目 | 執行結果 |", "|---|:---:|",
        ]
        global_icons = {"completed": "✅ 成功", "running": "🔄 執行中", "failed": "❌ 失敗", "paused": "⏸️ 暫停", "pending": "⏳ 等待"}
        for step in GLOBAL_STEPS:
            status = self.data["global_steps"].get(step, {}).get("status", "pending")
            lines.append(f"| {GLOBAL_LABELS[step]} | {global_icons[status]} |")
        lines.extend([
            "", "## 後製與 YouTube 發布狀態", "",
            "| 影片編號 | 小說章節 | 上傳成功 Slot | 準備章節 | 產生字幕 | 合併影片 | 驗證影片 | 資料／封面 | HF 備份 | 上傳影片 | 上傳封面 | 上傳字幕 | 播放清單 | 最終發布 | 最終驗收 |",
            "|---:|---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|",
        ])
        icons = {"completed": "✅", "running": "🔄", "failed": "❌", "paused": "⏸️", "pending": "⏳"}
        for planned in plan:
            part = self.data["parts"][str(planned["part_num"])]
            cells = [icons.get(part["steps"][step].get("status"), "⏳") for step in PART_STEPS]
            upload_slot = part["steps"]["upload_video"].get("youtube_slot")
            slot_label = f"slot{upload_slot}" if upload_slot else "—"
            lines.append(
                f"| Part {planned['part_num']:02d} | 第 {planned['start_chap']}–{planned['end_chap']} 章 | {slot_label} | "
                + " | ".join(cells) + " |"
            )
        failures = []
        for planned in plan:
            part = self.data["parts"][str(planned["part_num"])]
            step = part.get("resume_from")
            if step:
                record = part["steps"][step]
                failures.append((planned["part_num"], PART_LABELS[step], record.get("error") or "尚未執行", record.get("attempts", 0)))
        if failures:
            lines.extend(["", "### 斷點與待辦", "", "| Part | 續做位置 | 原因 | 嘗試次數 |", "|---:|---|---|---:|"])
            for number, step, reason, attempts in failures:
                lines.append(f"| Part {number:02d} | {step} | {str(reason).replace('|', '/')} | {attempts} |")
        return "\n".join(lines) + "\n"
