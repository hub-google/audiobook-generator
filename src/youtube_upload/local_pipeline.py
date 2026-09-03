"""Local prepared parts publishing pipeline."""

import glob
import json
import logging
import os
import re
import sys
from typing import Any

from .errors import UploadPaused, VideoNotFoundError
from .media import (
    _file_sha256,
    set_video_thumbnail,
    upload_video_file,
)
from .metadata import build_video_description
from .planning import parse_chapter_info
from .playlists import add_video_to_playlist
from .service_pool import EXIT_RETRY_LATER
from .state import recover_completed_titles_from_playlist, save_resume_state

try:
    from ..artifact_validation import validate_image, validate_srt, validate_video
    from ..metadata_gen import save_book_metadata
    from ..part_builder import get_media_duration, merge_part_videos
except ImportError:
    from artifact_validation import validate_image, validate_srt, validate_video
    from metadata_gen import save_book_metadata
    from part_builder import get_media_duration, merge_part_videos


def run_local_prepared_parts_mode(
    youtube, publication, hf_archiver, hf_executor, args, playlist_id,
    completed_titles, existing_titles, existing_video_ids, part_plan,
    pending_thumbnails, pending_playlist, pending_captions, pending_publish,
    book_title, start_chap, end_chap, config_path, hf_repo,
    temp_parts_dir, upload_subtitles_dir,
):
    prepared_plan = {}
    prepared_plan_path = os.path.join(args.input_dir, "parts-plan.json")
    if os.path.exists(prepared_plan_path):
        with open(prepared_plan_path, "r", encoding="utf-8") as handle:
            prepared_plan = json.load(handle)
    locked_parts = {
        int(part["part_num"]): part for part in prepared_plan.get("parts") or []
    }
    files_to_upload = sorted(glob.glob(os.path.join(args.input_dir, "**", "*.mp4"), recursive=True), key=lambda p: parse_chapter_info(os.path.basename(p)))
    if not files_to_upload:
        logging.error("❌ 未找到任何可供上傳的 MP4 影片檔案！")
        return [], part_plan, 0, 1

    is_part_files = any("_Part_" in os.path.basename(p) for p in files_to_upload)
    parts_to_upload = []

    if not is_part_files:
        from part_builder import partition_chapters
        partitioned_parts = partition_chapters(files_to_upload, min_hours=10.0, max_hours=11.0)
        for p in partitioned_parts:
            part_num = p["part_num"]
            s_c = p["start_chap"]
            e_c = p["end_chap"]
            out_name = f"{book_title}_Part_{part_num:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.mp4"
            out_path = os.path.join(temp_parts_dir, out_name)
            if merge_part_videos(p, out_path):
                p_meta = save_book_metadata(
                    book_title=book_title,
                    start_chap=s_c,
                    end_chap=e_c,
                    is_completed=True,
                    part_num=part_num,
                )
                parts_to_upload.append({
                    "video_path": out_path,
                    "title": p_meta["title"],
                    "description": p_meta["description"],
                    "cover_path": p_meta["cover_file"],
                    "master_cover_path": p_meta["master_cover_file"],
                    "part_num": part_num,
                    "start_chap": s_c,
                    "end_chap": e_c,
                    "chapter_timeline": p["items"],
                })
    else:
        for idx, vp in enumerate(files_to_upload, 1):
            c_start, c_end = parse_chapter_info(os.path.basename(vp))
            number_match = re.search(r"_Part_(\d+)_", os.path.basename(vp))
            part_number = int(number_match.group(1)) if number_match else idx
            locked = locked_parts.get(part_number, {})
            if locked and (int(locked["start_chap"]), int(locked["end_chap"])) != (c_start, c_end):
                raise RuntimeError(f"prepared Part {part_number} filename disagrees with locked plan")
            p_meta = save_book_metadata(
                book_title=book_title,
                start_chap=c_start,
                end_chap=c_end,
                is_completed=True,
                part_num=part_number,
            )
            parts_to_upload.append({
                "video_path": vp,
                "title": p_meta["title"],
                "description": p_meta["description"],
                "cover_path": p_meta["cover_file"],
                "master_cover_path": p_meta["master_cover_file"],
                "part_num": part_number,
                "start_chap": c_start,
                "end_chap": c_end,
                "chapters": [int(value) for value in locked.get("chapters") or range(c_start, c_end + 1)],
                "chapter_timeline": list(locked.get("chapter_timeline") or []),
                "source_missing_chapters": [int(value) for value in locked.get("source_missing_chapters") or []],
            })

    local_plan = []
    if locked_parts and {int(item["part_num"]) for item in parts_to_upload} != set(locked_parts):
        raise RuntimeError("HF prepared Parts do not exactly cover the locked Part plan")
    for item in parts_to_upload:
        duration = float(get_media_duration(item["video_path"]) or 0)
        if duration <= 0:
            raise RuntimeError(f"prepared Part {item['part_num']} has no measured MP4 duration")
        local_plan.append({
            "part_num": item["part_num"], "start_chap": item["start_chap"],
            "end_chap": item["end_chap"],
            "chapters": item.get("chapters") or list(range(item["start_chap"], item["end_chap"] + 1)),
            "duration": duration, "title": item["title"],
        })
    artifact_count = len(set(prepared_plan.get("chapter_artifacts", {}).values())) if prepared_plan.get("chapter_artifacts") else len(files_to_upload)
    chapter_count = len(prepared_plan.get("chapter_artifacts", {})) if prepared_plan.get("chapter_artifacts") else sum(len(p.get("chapters", [])) for p in local_plan)
    source_missing = prepared_plan.get("source_missing_chapters", []) if prepared_plan else []
    publication.mark_global("download_artifacts", "completed", artifact_count=artifact_count)
    publication.mark_global("probe_durations", "completed", chapter_count=chapter_count)
    publication.mark_global("validate_inventory", "completed", chapter_count=chapter_count, source_missing_chapters=source_missing)
    if publication.is_locked() and publication.data.get("plan"):
        base_plan = publication.data["plan"]
        publication.mark_global("lock_plan", "completed", part_count=len(base_plan))
    else:
        base_plan = publication.lock_plan(local_plan, run_id=args.run_id, book_title=book_title)
        publication.mark_global("lock_plan", "completed", part_count=len(base_plan))

    measured_durations = {int(item["part_num"]): float(item["duration"]) for item in local_plan}
    locked_part_nums = {int(p["part_num"]) for p in base_plan}
    measured_part_nums = set(measured_durations.keys())
    if measured_part_nums != locked_part_nums:
        raise RuntimeError(
            f"Measured parts {sorted(measured_part_nums)} do not match locked plan parts {sorted(locked_part_nums)}"
        )
    if any(d <= 0 for d in measured_durations.values()):
        raise RuntimeError("缺少全部影片的實測時長，任一 Part 時長必須大於 0")

    part_plan = [dict(p) for p in base_plan]
    for p in part_plan:
        p["duration"] = measured_durations[int(p["part_num"])]

    recovered_titles = recover_completed_titles_from_playlist(
        completed_titles,
        existing_titles,
        (item["title"] for item in parts_to_upload),
    )
    if recovered_titles:
        logging.warning(
            "Recovered %s/%s completed Parts from exact title matches in the target playlist; "
            "they will not be uploaded again.",
            len(recovered_titles), len(parts_to_upload),
        )

    total_uploaded = 0

    for idx, item in enumerate(parts_to_upload, 1):
        v_path = item["video_path"]
        v_title = item["title"]
        v_desc = item["description"]
        v_cover = item["cover_path"]
        part_n = item["part_num"]

        if v_title in existing_titles and v_title in completed_titles:
            logging.info("⏭️ 已存在於播放清單，跳過：%s", v_title)
            preexisting_video_id = existing_video_ids.get(v_title)
            if part_n in hf_archiver.completed_parts(book_title):
                publication.complete(part_n, "archive_hf", recovered_from_hf=True, hf_repo=hf_repo)
            elif preexisting_video_id:
                v_srt_name = os.path.basename(v_path).replace(".mp4", ".srt")
                v_srt = os.path.join(upload_subtitles_dir, v_srt_name)
                if not os.path.exists(v_srt):
                    v_srt = v_path.replace(".mp4", ".srt")
                    if not os.path.exists(v_srt):
                        v_srt = None
                full_desc = build_video_description(
                    book_title, v_desc, playlist_id, item.get("chapter_timeline"),
                )
                publication.mark(part_n, "archive_hf", "running")
                archive_method = (
                    hf_archiver.register_preuploaded_part
                    if os.environ.get("HF_MEDIA_PREUPLOADED", "").lower() in {"1", "true", "yes"}
                    else hf_archiver.archive_part
                )
                archive_record = archive_method(
                    book_title=book_title, part_num=part_n,
                    start_chap=item["start_chap"], end_chap=item["end_chap"],
                    chapters=item.get("chapters") or list(range(item["start_chap"], item["end_chap"] + 1)),
                    video_path=v_path, subtitle_path=v_srt,
                    master_cover_path=item["master_cover_path"],
                    source_config_path=config_path, run_id=args.run_id,
                    task_id=args.task_id,
                    source_missing_chapters=item.get("source_missing_chapters") or [],
                )
                archive_record = hf_archiver.finalize_part(
                    book_title=book_title, part_num=part_n,
                    youtube_video_id=preexisting_video_id, playlist_id=playlist_id,
                    title=v_title, description=full_desc, privacy=args.privacy,
                    playlist_position=part_n - 1,
                )
                publication.complete(part_n, "archive_hf", hf_repo=hf_repo, path=archive_record["root"])
            else:
                if part_n in hf_archiver.completed_parts(book_title):
                    publication.complete(part_n, "archive_hf", recovered_from_hf=True, hf_repo=hf_repo)
            continue

        v_srt_name = os.path.basename(v_path).replace(".mp4", ".srt")
        v_srt = os.path.join(upload_subtitles_dir, v_srt_name)
        if not os.path.exists(v_srt):
            v_srt = v_path.replace(".mp4", ".srt")
            if not os.path.exists(v_srt):
                v_srt = None
        if not v_srt:
            logging.error("Required CC subtitle file is missing; stopping before upload: %s", v_title)
            return parts_to_upload, part_plan, total_uploaded, 1
        publication.complete(part_n, "prepare_chapters", chapter_count=item["end_chap"] - item["start_chap"] + 1)
        publication.complete(part_n, "generate_subtitle", **validate_srt(v_srt, get_media_duration(v_path)))
        publication.complete(part_n, "merge_video", output=v_path, reused_existing=True)
        publication.complete(part_n, "validate_video", **validate_video(v_path))
        cover_validation = validate_image(v_cover, expected_size=(1280, 720))
        publication.complete(part_n, "generate_metadata_cover", title=v_title, cover_sha256=cover_validation["sha256"])

        full_desc = build_video_description(
            book_title, v_desc, playlist_id, item.get("chapter_timeline"),
        )
        publication.mark(part_n, "archive_hf", "running")
        archive_method = (
            hf_archiver.register_preuploaded_part
            if os.environ.get("HF_MEDIA_PREUPLOADED", "").lower() in {"1", "true", "yes"}
            else hf_archiver.archive_part
        )
        archive_record = archive_method(
            book_title=book_title, part_num=part_n,
            start_chap=item["start_chap"], end_chap=item["end_chap"],
            chapters=item.get("chapters") or list(range(item["start_chap"], item["end_chap"] + 1)),
            video_path=v_path, subtitle_path=v_srt,
            master_cover_path=item["master_cover_path"],
            source_config_path=config_path, run_id=args.run_id,
            task_id=args.task_id,
            source_missing_chapters=item.get("source_missing_chapters") or [],
        )
        logging.info(f"[API_UPLOAD_MARKER] START | Part {part_n}/{len(parts_to_upload)} | Ch {item['start_chap']}~{item['end_chap']} | {os.path.basename(v_path)}")
        try:
            publication.mark(part_n, "upload_video", "running")
            v_id = upload_video_file(
                youtube,
                video_path=v_path,
                title=v_title,
                description=full_desc,
                privacy_status="public",
                cover_path=None,
            )
        except UploadPaused as paused:
            publication.fail(part_n, "upload_video", paused, paused=True)
            save_resume_state(args.state_file, args.run_id, args.privacy, "paused",
                              paused.reason, paused.retry_at, completed_titles, part_plan,
                              pending_thumbnails, playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None)
            logging.error(
                "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                len(completed_titles), len(part_plan), paused.retry_at.isoformat(), args.run_id, paused.reason,
            )
            return parts_to_upload, part_plan, total_uploaded, EXIT_RETRY_LATER
        if v_id:
            publication.record_upload_ack(
                part_n, v_id, _file_sha256(v_path),
                youtube_slot=youtube.active_account["slot"],
            )
            pending_thumbnails[v_title] = v_id
            pending_playlist[v_title] = v_id
            save_resume_state(
                args.state_file, args.run_id, "public", "running",
                completed_titles=completed_titles, part_plan=part_plan,
                pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
            )
            part_rec = publication.data.get("parts", {}).get(str(part_n), {})
            thumb_step = (part_rec.get("steps") or {}).get("upload_thumbnail") or {}
            if thumb_step.get("status") != "completed":
                try:
                    set_video_thumbnail(youtube, v_id, v_cover)
                    publication.record_thumbnail_ack(part_n)
                    pending_thumbnails.pop(v_title, None)
                    logging.info("[YouTube Quota] thumbnail: +50")
                except VideoNotFoundError as not_found:
                    logging.error("❌ 剛上傳的影片已被 YouTube 移除或無法找到 (Video ID: %s): %s", v_id, not_found)
                    publication.reset_upload(part_n, reason=f"videoNotFound:{v_id}")
                    pending_thumbnails.pop(v_title, None)
                    pending_playlist.pop(v_title, None)
                    completed_titles.discard(v_title)
                    existing_titles.discard(v_title)
                    save_resume_state(
                        args.state_file, args.run_id, args.privacy, "running",
                        completed_titles=completed_titles, part_plan=part_plan,
                        pending_thumbnails=pending_thumbnails,
                        pending_playlist=pending_playlist,
                        pending_captions=pending_captions,
                        pending_publish=pending_publish,
                        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                    )
                    continue
                except UploadPaused as paused:
                    publication.fail(
                        part_n, "upload_thumbnail", paused,
                        paused=True, youtube_video_id=v_id,
                    )
                    save_resume_state(
                        args.state_file, args.run_id, args.privacy, "paused",
                        paused.reason, paused.retry_at, completed_titles, part_plan,
                        pending_thumbnails, pending_playlist=pending_playlist,
                        pending_captions=pending_captions,
                        pending_publish=pending_publish,
                        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                    )
                    logging.error(
                        "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | "
                        "retry_at=%s | source_run=%s | reason=%s",
                        len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
                        args.run_id, paused.reason,
                    )
                    return parts_to_upload, part_plan, total_uploaded, EXIT_RETRY_LATER
            else:
                pending_thumbnails.pop(v_title, None)
            part_rec = publication.data.get("parts", {}).get(str(part_n), {})
            playlist_rec = part_rec.get("playlist") or {}
            playlist_step = (part_rec.get("steps") or {}).get("add_playlist") or {}
            has_pl_ack = (
                playlist_rec.get("status") == "completed"
                or playlist_rec.get("playlist_item_id")
                or playlist_step.get("status") == "completed"
            )
            if has_pl_ack:
                logging.info("⏭️ Part %s 播放清單已於 Checkpoint 標記完成；跳過 insert：%s", part_n, v_title)
                pending_playlist.pop(v_title, None)
            else:
                playlist_item_id = add_video_to_playlist(
                    youtube, playlist_id, v_id, position=part_n - 1,
                )
                if not playlist_item_id:
                    raise RuntimeError("playlistItems.insert returned no acknowledgement")
                publication.record_playlist_ack(part_n, playlist_item_id, part_n - 1)
                pending_playlist.pop(v_title, None)
            publication.complete(part_n, "final_validation", deferred_to_final_audit=True)
            completed_titles.add(v_title)
            existing_titles.add(v_title)
            archive_record = hf_archiver.finalize_part(
                book_title=book_title, part_num=part_n,
                youtube_video_id=v_id, playlist_id=playlist_id,
                title=v_title, description=full_desc, privacy="public",
                playlist_position=part_n - 1,
            )
            publication.complete(part_n, "archive_hf", hf_repo=hf_repo, path=archive_record["root"])
            save_resume_state(
                args.state_file, args.run_id, "public", "running",
                completed_titles=completed_titles, part_plan=part_plan,
                pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
            )
            logging.info("[YouTube Quota] playlist insert: +50; estimated general quota used: 100")
            total_uploaded += 1

    return parts_to_upload, part_plan, total_uploaded, 0
