"""CI artifact download, multi-worker inventorying, merging, and upload pipeline."""

import glob
import logging
import os
import shutil
import sys
import threading
from typing import Any

from .errors import UploadPaused
from .media import (
    _file_sha256,
    generate_part_srt,
    set_video_thumbnail,
    upload_video_file,
)
from .metadata import build_video_description
from .planning import (
    build_part_plan_from_inventory,
    download_artifact_task,
    get_run_artifact_names,
    scan_artifact_chapters,
    validate_chapter_inventory,
)
from .playlists import add_video_to_playlist
from .service_pool import EXIT_RETRY_LATER
from .state import recover_completed_titles_from_playlist, save_resume_state
from .verification import verify_published_part

try:
    from ..artifact_validation import validate_image, validate_srt, validate_video
    from ..metadata_gen import generate_video_title, save_book_metadata
    from ..part_builder import get_media_duration, merge_part_videos, parse_chapter_num
    from ..source_status import confirmed_missing_from_directory
except ImportError:
    from artifact_validation import validate_image, validate_srt, validate_video
    from metadata_gen import generate_video_title, save_book_metadata
    from part_builder import get_media_duration, merge_part_videos, parse_chapter_num
    from source_status import confirmed_missing_from_directory


def run_ci_artifact_mode(
    youtube, publication, hf_archiver, hf_executor, args, playlist_id,
    completed_titles, existing_titles, existing_video_ids, part_plan,
    pending_thumbnails, pending_playlist, pending_captions, pending_publish,
    book_title, start_chap, end_chap, config_path, hf_repo,
    temp_parts_dir, upload_subtitles_dir, temp_dl_dir,
):
    logging.info("📥 啟動【相容模式：下載 ➔ 合併 ➔ 發布】...")
    logging.info(f"Target Run ID #{args.run_id}")

    artifact_names = get_run_artifact_names(args.run_id, args.repo)
    if not artifact_names:
        logging.error(f"❌ 未在 Run #{args.run_id} 中找到任何影片 Artifacts！")
        return None, 0, 1

    logging.info(f"共有 {len(artifact_names)} 個 Worker Artifacts 待處理。")

    min_seconds = 10.0 * 3600.0
    max_seconds = 11.0 * 3600.0

    inventory = []
    confirmed_source_missing = set()
    publication.mark_global("download_artifacts", "running")
    logging.info("🔎 第一階段：盤點全部 Worker Artifacts；此階段不會上傳任何影片。")
    for inventory_index, artifact_name in enumerate(artifact_names, 1):
        inventory_dir = os.path.join(temp_dl_dir, f"inventory-{artifact_name}")
        logging.info("📦 盤點 [%s/%s]：%s", inventory_index, len(artifact_names), artifact_name)
        if not download_artifact_task(args.run_id, args.repo, artifact_name, inventory_dir):
            raise RuntimeError(f"Artifact 下載失敗，禁止上傳：{artifact_name}")
        artifact_missing = confirmed_missing_from_directory(inventory_dir)
        confirmed_source_missing.update(artifact_missing)
        scanned = scan_artifact_chapters(inventory_dir, artifact_name)
        if not scanned and not artifact_missing:
            raise RuntimeError(f"Artifact 沒有章節 MP4，禁止上傳：{artifact_name}")
        inventory.extend(scanned)
        shutil.rmtree(inventory_dir, ignore_errors=True)

    publication.mark_global("download_artifacts", "completed", artifact_count=len(artifact_names))
    publication.mark_global("probe_durations", "completed", chapter_count=len(inventory))

    inventory.sort(key=lambda item: int(item["chap_num"]))
    publication.mark_global("validate_inventory", "running")
    inventory_result = validate_chapter_inventory(
        inventory, start_chap, end_chap, confirmed_source_missing
    )
    publication.mark_global(
        "validate_inventory", "completed", chapter_count=len(inventory),
        source_missing_chapters=inventory_result["confirmed_missing"],
    )
    candidate_plan = build_part_plan_from_inventory(
        inventory, min_seconds, max_seconds, confirmed_source_missing
    )
    for planned in candidate_plan:
        planned["title"] = generate_video_title(
            book_title,
            start_chap=planned["start_chap"],
            end_chap=planned["end_chap"],
            part_num=planned["part_num"],
        )
        logging.info(
            "🧭 Part %02d：第 %04d~%04d 章，共 %d 章，%.2f 小時",
            planned["part_num"], planned["start_chap"], planned["end_chap"],
            len(planned["chapters"]), planned["duration"] / 3600,
        )
    publication.lock_plan(candidate_plan, run_id=args.run_id, book_title=book_title)
    publication.mark_global("lock_plan", "completed", part_count=len(candidate_plan))
    part_plan = candidate_plan
    recovered_titles = recover_completed_titles_from_playlist(
        completed_titles,
        existing_titles,
        (planned["title"] for planned in part_plan),
    )
    if recovered_titles:
        logging.warning(
            "Recovered %s/%s completed Parts from exact title matches in the target playlist; "
            "they will not be uploaded again.",
            len(recovered_titles), len(part_plan),
        )
    leading_gap = set(range(start_chap, part_plan[0]["start_chap"]))
    if leading_gap and not leading_gap.issubset(confirmed_source_missing):
        raise RuntimeError("Part 1 並非從全書第一章開始，禁止上傳")
    save_resume_state(
        args.state_file, args.run_id, args.privacy, "planned",
        completed_titles=completed_titles, part_plan=part_plan,
        pending_thumbnails=pending_thumbnails,
        pending_playlist=pending_playlist,
        pending_captions=pending_captions,
        pending_publish=pending_publish,
        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
    )
    logging.info("✅ 全書分部規劃已鎖定；相容模式將依 Part 1 → Part %s 發布。", len(part_plan))

    chapter_pool = []
    art_dir_first = os.path.join(temp_dl_dir, artifact_names[0])
    logging.info(f"📦 [1/{len(artifact_names)}] 下載 Artifact: {artifact_names[0]}...")
    download_artifact_task(args.run_id, args.repo, artifact_names[0], art_dir_first)

    prefetch_thread = None
    part_counter = 1
    total_uploaded = 0

    for a_idx, a_name in enumerate(artifact_names):
        art_dir = os.path.join(temp_dl_dir, a_name)

        if a_idx + 1 < len(artifact_names):
            next_name = artifact_names[a_idx + 1]
            next_dir = os.path.join(temp_dl_dir, next_name)
            logging.info(f"⚡ 【異步背景預載】發動背景線程預先下載下一個包 [{a_idx+2}/{len(artifact_names)}]: {next_name}...")
            prefetch_thread = threading.Thread(
                target=download_artifact_task,
                args=(args.run_id, args.repo, next_name, next_dir)
            )
            prefetch_thread.start()
        else:
            prefetch_thread = None

        m_files = glob.glob(os.path.join(art_dir, "**", "*.mp4"), recursive=True)
        for f_path in m_files:
            if not any(x["path"] == f_path for x in chapter_pool):
                c_num = parse_chapter_num(os.path.basename(f_path))
                dur = get_media_duration(f_path)

                srt_path = f_path.replace("/Video/", "/Subtitles/").replace("\\Video\\", "\\Subtitles\\").replace(".mp4", ".srt")
                if not os.path.exists(srt_path):
                    parent_art = os.path.dirname(os.path.dirname(f_path))
                    srt_matches = glob.glob(os.path.join(parent_art, "**", f"*chapter_{c_num}.srt"), recursive=True)
                    srt_path = srt_matches[0] if srt_matches else None

                chapter_pool.append({
                    "path": f_path,
                    "srt_path": srt_path,
                    "chap_num": c_num,
                    "dur": dur,
                })

        chapter_pool.sort(key=lambda x: x["chap_num"])
        is_last_artifact = (a_idx == len(artifact_names) - 1)

        while True:
            if not chapter_pool:
                break

            pool_dur = sum(x["dur"] for x in chapter_pool)
            if pool_dur <= max_seconds and not is_last_artifact:
                logging.info(f"   目前累計 {len(chapter_pool)} 章 (約 {pool_dur/3600:.2f} 小時)，等待足夠章節以固定 11 小時邊界...")
                break

            sliced_items = []
            sliced_dur = 0.0
            locked_part = next(
                (p for p in part_plan if int(p.get("part_num", 0)) == part_counter),
                None,
            )

            if locked_part:
                locked_chapters = [int(n) for n in locked_part.get("chapters", [])]
                available = {int(item["chap_num"]): item for item in chapter_pool}
                missing = [n for n in locked_chapters if n not in available]
                if missing and not is_last_artifact:
                    logging.info(
                        "鎖定的第 %s 部尚缺章節 %s，繼續下載下一個 Artifact。",
                        part_counter, missing[:8],
                    )
                    break
                if missing:
                    raise RuntimeError(
                        f"斷點規劃損壞：第 {part_counter} 部缺少章節 {missing}"
                    )
                sliced_items = [available[n] for n in locked_chapters]
                sliced_dur = sum(item["dur"] for item in sliced_items)
            else:
                for item in chapter_pool:
                    if sliced_items and (sliced_dur + item["dur"] > max_seconds):
                        break
                    sliced_items.append(item)
                    sliced_dur += item["dur"]
                    if sliced_dur >= max_seconds:
                        break

            if not sliced_items:
                break

            s_c = sliced_items[0]["chap_num"]
            e_c = sliced_items[-1]["chap_num"]
            expected_title = generate_video_title(book_title, start_chap=s_c, end_chap=e_c, part_num=part_counter)

            if not locked_part:
                locked_part = {
                    "part_num": part_counter,
                    "start_chap": s_c,
                    "end_chap": e_c,
                    "chapters": [int(item["chap_num"]) for item in sliced_items],
                    "title": expected_title,
                }
                part_plan.append(locked_part)
                save_resume_state(
                    args.state_file, args.run_id, args.privacy, "running",
                    completed_titles=completed_titles, part_plan=part_plan,
                    pending_thumbnails=pending_thumbnails,
                )

            preexisting_video_id = existing_video_ids.get(expected_title) if (expected_title in existing_titles and expected_title in completed_titles) else None

            if expected_title in existing_titles and expected_title in completed_titles and part_counter in hf_archiver.completed_parts(book_title):
                logging.info(f"⏭️ 【第 {part_counter} 部】(第 {s_c}~{e_c} 章) 已存在於 YouTube 播放清單，觸發【智能斷點續傳】秒跳過！")
                publication.complete(part_counter, "archive_hf", recovered_from_hf=True, hf_repo=hf_repo)
                for item in sliced_items:
                    try:
                        if os.path.exists(item["path"]):
                            os.remove(item["path"])
                    except Exception:
                        pass
                sliced_paths = set(x["path"] for x in sliced_items)
                chapter_pool = [x for x in chapter_pool if x["path"] not in sliced_paths]
                part_counter += 1
                continue

            out_name = f"{book_title}_Part_{part_counter:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.mp4"
            out_srt_name = f"{book_title}_Part_{part_counter:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.srt"
            out_path = os.path.join(temp_parts_dir, out_name)
            out_srt_path = os.path.join(upload_subtitles_dir, out_srt_name)

            publication.complete(
                part_counter, "prepare_chapters",
                chapter_count=len(sliced_items), expected_duration_seconds=round(sliced_dur, 3),
            )
            publication.mark(part_counter, "generate_subtitle", "running")
            srt_ok = generate_part_srt(sliced_items, out_srt_path)
            if not srt_ok or not os.path.exists(out_srt_path):
                publication.fail(part_counter, "generate_subtitle", RuntimeError("Part SRT was not generated"))
                logging.error("Required Part CC subtitle file was not generated; stopping before upload")
                return part_plan, total_uploaded, 1
            srt_validation = validate_srt(out_srt_path, sliced_dur)
            publication.complete(part_counter, "generate_subtitle", **srt_validation)

            part_info = {
                "part_num": part_counter,
                "start_chap": s_c,
                "end_chap": e_c,
                "files": [x["path"] for x in sliced_items],
                "duration": sliced_dur,
            }

            logging.info(f"\n🚀 【第 {part_counter} 部】準備無縫合成: 第 {s_c}~{e_c} 章 ({len(sliced_items)} 章，總長 {sliced_dur/3600:.2f} 小時)")

            publication.mark(part_counter, "merge_video", "running")
            if merge_part_videos(part_info, out_path):
                publication.complete(part_counter, "merge_video", output=out_path)
                publication.mark(part_counter, "validate_video", "running")
                video_validation = validate_video(out_path, sliced_dur)
                publication.complete(part_counter, "validate_video", **video_validation)
                publication.mark(part_counter, "generate_metadata_cover", "running")
                p_meta = save_book_metadata(
                    book_title=book_title,
                    start_chap=s_c,
                    end_chap=e_c,
                    is_completed=True,
                    part_num=part_counter,
                )
                cover_validation = validate_image(p_meta["cover_file"], expected_size=(1280, 720))
                if cover_validation["bytes"] >= 2 * 1024 * 1024:
                    raise RuntimeError("YouTube cover exceeds the 2 MB limit")
                publication.complete(
                    part_counter, "generate_metadata_cover",
                    title=p_meta["title"], cover=p_meta["cover_file"], cover_sha256=cover_validation["sha256"],
                )
                full_desc = build_video_description(
                    book_title, p_meta["description"], playlist_id, sliced_items,
                )
                omitted = [int(value) for value in locked_part.get("source_missing_chapters", [])]

                publication.mark(part_counter, "archive_hf", "running")
                hf_future = hf_executor.submit(
                    hf_archiver.archive_part,
                    book_title=book_title, part_num=part_counter,
                    start_chap=s_c, end_chap=e_c,
                    chapters=[int(item["chap_num"]) for item in sliced_items],
                    video_path=out_path, subtitle_path=out_srt_path,
                    master_cover_path=p_meta["master_cover_file"],
                    source_config_path=config_path, run_id=args.run_id,
                    task_id=args.task_id, source_missing_chapters=omitted,
                )

                if preexisting_video_id:
                    try:
                        hf_future.result()
                        archive_record = hf_archiver.finalize_part(
                            book_title=book_title, part_num=part_counter,
                            youtube_video_id=preexisting_video_id, playlist_id=playlist_id,
                            title=p_meta["title"], description=full_desc,
                            privacy=args.privacy, playlist_position=part_counter - 1,
                        )
                        publication.complete(part_counter, "archive_hf", hf_repo=hf_repo, path=archive_record["root"])
                        logging.info("[HF_ARCHIVE_MARKER] DONE | Part %s | Ch %s~%s | %s", part_counter, s_c, e_c, archive_record["root"])
                        for step in ("upload_video", "upload_thumbnail", "upload_caption", "add_playlist", "publish"):
                            publication.complete(part_counter, step, youtube_video_id=preexisting_video_id, recovered=True)
                        publication.complete(
                            part_counter, "final_validation",
                            **verify_published_part(
                                youtube, preexisting_video_id, playlist_id, args.privacy,
                                srt_path=out_srt_path,
                                part_title=p_meta["title"],
                                part_num=part_counter,
                                playlist_position=part_counter - 1,
                            ),
                        )
                    except Exception as error:
                        publication.fail(part_counter, "archive_hf", error)
                        logging.error("HF archive recovery failed; retaining Part media: %s", error)
                        return part_plan, total_uploaded, 1
                    for item in sliced_items:
                        if os.path.exists(item["path"]):
                            os.remove(item["path"])
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    sliced_paths = {item["path"] for item in sliced_items}
                    chapter_pool = [item for item in chapter_pool if item["path"] not in sliced_paths]
                    part_counter += 1
                    continue

                logging.info(f"[API_UPLOAD_MARKER] START | Part {part_counter} | Ch {s_c}~{e_c} | {out_name}")
                sys.stdout.flush()

                try:
                    publication.mark(part_counter, "upload_video", "running")
                    v_id = upload_video_file(
                        youtube,
                        video_path=out_path,
                        title=p_meta["title"],
                        description=full_desc,
                        privacy_status="public",
                        cover_path=None,
                    )
                except UploadPaused as paused:
                    publication.fail(part_counter, "upload_video", paused, paused=True)
                    save_resume_state(args.state_file, args.run_id, args.privacy, "paused",
                                      paused.reason, paused.retry_at, completed_titles, part_plan,
                                      pending_thumbnails)
                    logging.error(
                        "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                        len(completed_titles), len(part_plan), paused.retry_at.isoformat(), args.run_id, paused.reason,
                    )
                    logging.error("⏸️ 已安全暫停；下次可重試時間：%s", paused.retry_at.isoformat())
                    return part_plan, total_uploaded, EXIT_RETRY_LATER

                if v_id:
                    publication.record_upload_ack(
                        part_counter, v_id, _file_sha256(out_path),
                        youtube_slot=youtube.active_account["slot"],
                    )
                    pending_thumbnails[p_meta["title"]] = v_id
                    pending_playlist[p_meta["title"]] = v_id
                    save_resume_state(args.state_file, args.run_id, "public", "running",
                                      completed_titles=completed_titles, part_plan=part_plan,
                                      pending_thumbnails=pending_thumbnails,
                                      pending_playlist=pending_playlist)
                    part_rec = publication.data.get("parts", {}).get(str(part_counter), {})
                    thumb_step = (part_rec.get("steps") or {}).get("upload_thumbnail") or {}
                    if thumb_step.get("status") != "completed":
                        try:
                            set_video_thumbnail(youtube, v_id, p_meta["cover_file"])
                            publication.record_thumbnail_ack(part_counter)
                            pending_thumbnails.pop(p_meta["title"], None)
                            logging.info("[YouTube Quota] thumbnail: +50")
                        except UploadPaused as paused:
                            publication.fail(
                                part_counter, "upload_thumbnail", paused,
                                paused=True, youtube_video_id=v_id,
                            )
                            save_resume_state(
                                args.state_file, args.run_id, args.privacy, "paused",
                                paused.reason, paused.retry_at, completed_titles, part_plan,
                                pending_thumbnails, pending_playlist=pending_playlist,
                                pending_captions=pending_captions,
                                pending_publish=pending_publish,
                                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None,
                            )
                            logging.error(
                                "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | "
                                "retry_at=%s | source_run=%s | reason=%s",
                                len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
                                args.run_id, paused.reason,
                            )
                            return part_plan, total_uploaded, EXIT_RETRY_LATER
                    else:
                        pending_thumbnails.pop(p_meta["title"], None)
                    playlist_item_id = add_video_to_playlist(
                        youtube, playlist_id, v_id, position=part_counter - 1,
                    )
                    if not playlist_item_id:
                        raise RuntimeError("playlistItems.insert returned no acknowledgement")
                    publication.record_playlist_ack(part_counter, playlist_item_id, part_counter - 1)
                    pending_playlist.pop(p_meta["title"], None)
                    publication.complete(part_counter, "final_validation", deferred_to_final_audit=True)
                    hf_future.result()
                    archive_record = hf_archiver.finalize_part(
                        book_title=book_title, part_num=part_counter,
                        youtube_video_id=v_id, playlist_id=playlist_id,
                        title=p_meta["title"], description=full_desc,
                        privacy="public", playlist_position=part_counter - 1,
                    )
                    publication.complete(part_counter, "archive_hf", hf_repo=hf_repo, path=archive_record["root"])
                    completed_titles.add(p_meta["title"])
                    existing_titles.add(p_meta["title"])
                    save_resume_state(args.state_file, args.run_id, "public", "running",
                                      completed_titles=completed_titles, part_plan=part_plan,
                                      pending_thumbnails=pending_thumbnails,
                                      pending_playlist=pending_playlist,
                                      playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}")
                    logging.info("[YouTube Quota] playlist insert: +50; estimated general quota used: 50")
                    total_uploaded += 1
                    for completed_item in sliced_items:
                        if os.path.exists(completed_item["path"]):
                            os.remove(completed_item["path"])
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    sliced_paths = {completed_item["path"] for completed_item in sliced_items}
                    chapter_pool = [candidate for candidate in chapter_pool if candidate["path"] not in sliced_paths]
                    part_counter += 1
                    continue

                if not v_id:
                    logging.error("❌ 未取得 Video ID，保留所有檔案並中止，避免斷點被錯誤推進。")
                    return part_plan, total_uploaded, 1

                logging.info(f"🧹 釋放硬碟空間：清理【第 {part_counter} 部】已上傳完畢的 {len(sliced_items)} 個單章原始檔與 Part 大影片...")
                for item in sliced_items:
                    try:
                        if os.path.exists(item["path"]):
                            os.remove(item["path"])
                    except Exception as e:
                        logging.warning(f"刪除暫存檔 {item['path']} 失敗: {e}")

                if os.path.exists(out_path):
                    try:
                        os.remove(out_path)
                    except Exception:
                        pass

            sliced_paths = set(x["path"] for x in sliced_items)
            chapter_pool = [x for x in chapter_pool if x["path"] not in sliced_paths]
            part_counter += 1

            remaining_dur = sum(x["dur"] for x in chapter_pool)
            if remaining_dur <= max_seconds and not is_last_artifact:
                logging.info(f"   剩餘 {len(chapter_pool)} 章 (約 {remaining_dur/3600:.2f} 小時)，繼續下載以固定分部邊界...")
                break

        if prefetch_thread:
            prefetch_thread.join()

    return part_plan, total_uploaded, 0
