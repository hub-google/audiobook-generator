"""Final consistency audit, user-facing playlist validation, and archive reconciliation."""

import logging
import sys
import time
from typing import Any

from .errors import UploadPaused
from .playlists import (
    completed_playlist_title,
    get_channel_upload_video_index,
    get_ordered_playlist_items,
    update_playlist_metadata,
    validate_user_facing_playlist,
)
from .service_pool import EXIT_RETRY_LATER
from .state import save_resume_state

try:
    from ..publication_checkpoint import PART_STEPS
except ImportError:
    from publication_checkpoint import PART_STEPS


RECONCILIATION_DELAYS = (2, 4, 8, 15, 30)


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def run_final_playlist_and_archive_audit(
    youtube, publication, hf_archiver, hf_executor, args, playlist_id,
    playlist_desc, completed_titles, part_plan, pending_thumbnails,
    pending_playlist, pending_captions, pending_publish, book_title,
    hf_repo, total_uploaded,
):
    if pending_playlist:
        raise RuntimeError(f"仍有 {len(pending_playlist)} 部影片未加入播放清單，禁止標記 complete")
    if pending_captions:
        raise RuntimeError(f"仍有 {len(pending_captions)} 部影片缺少 YouTube CC 字幕，禁止標記 complete")
    if pending_publish:
        raise RuntimeError(f"仍有 {len(pending_publish)} 部影片尚未完成最終發布，禁止標記 complete")

    final_playlist_validation = None
    if args.run_id:
        expected_titles = {str(part.get("title") or "").strip() for part in part_plan}
        expected_titles.discard("")
        finished_titles = completed_titles
        missing_titles = sorted(expected_titles - finished_titles)
        if missing_titles:
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "running",
                reason="incompleteParts", completed_titles=completed_titles,
                part_plan=part_plan, pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None
            )
            logging.error(
                "[API_UPLOAD_STATUS] INCOMPLETE | finished=%s | total=%s | missing=%s",
                len(expected_titles) - len(missing_titles), len(expected_titles), missing_titles[:3],
            )
            return EXIT_RETRY_LATER

        try:
            final_playlist_items = get_ordered_playlist_items(youtube, playlist_id)
            final_playlist_index = {
                item["title"]: item["video_id"] for item in final_playlist_items
                if item["title"] and item["video_id"]
            }
        except UploadPaused as paused:
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Final playlist index validation quota exhausted; retry after %s", paused.retry_at.isoformat())
            return EXIT_RETRY_LATER

        sleep_fn = _get_symbol("time", time).sleep

        def _refresh_playlist():
            nonlocal final_playlist_items, final_playlist_index
            final_playlist_items = get_ordered_playlist_items(youtube, playlist_id)
            final_playlist_index = {
                item["title"]: item["video_id"] for item in final_playlist_items
                if item["title"] and item["video_id"]
            }

        final_channel_index = None
        for planned in part_plan:
            title = str(planned.get("title") or "").strip()
            part_num = int(planned["part_num"])
            record = publication.data.get("parts", {}).get(str(part_num), {})
            known_video_id = (
                (record.get("upload") or {}).get("video_id")
                or (record.get("steps", {}).get("upload_video") or {}).get("youtube_video_id")
            )
            has_playlist_ack = bool(
                (record.get("playlist") or {}).get("status") == "completed"
                or (record.get("playlist") or {}).get("playlist_item_id")
                or ((record.get("steps") or {}).get("add_playlist") or {}).get("status") == "completed"
            )

            video_id = None
            if title in final_playlist_index:
                video_id = final_playlist_index[title]
            elif known_video_id and any(item.get("video_id") == known_video_id for item in final_playlist_items):
                video_id = known_video_id
                final_playlist_index[title] = video_id

            if not video_id:
                if known_video_id:
                    try:
                        v_res = youtube.videos().list(part="id,snippet", id=known_video_id).execute()
                        if v_res.get("items"):
                            video_id = known_video_id
                            logging.info(
                                "Final reconciliation verified video existence via known video ID: Part %s | video=%s",
                                part_num, video_id,
                            )
                    except UploadPaused as paused:
                        save_resume_state(
                            args.state_file, args.run_id, args.privacy, "paused",
                            paused.reason, paused.retry_at, completed_titles, part_plan,
                            pending_thumbnails,
                            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                            pending_playlist=pending_playlist,
                            pending_captions=pending_captions,
                            pending_publish=pending_publish,
                        )
                        return EXIT_RETRY_LATER
                    except Exception as e:
                        logging.debug("videos().list check failed for %s: %s", known_video_id, e)

                if not video_id:
                    for attempt, delay in enumerate(RECONCILIATION_DELAYS):
                        if final_channel_index is None:
                            try:
                                final_channel_index = get_channel_upload_video_index(youtube)
                            except UploadPaused as paused:
                                save_resume_state(
                                    args.state_file, args.run_id, args.privacy, "paused",
                                    paused.reason, paused.retry_at, completed_titles, part_plan,
                                    pending_thumbnails,
                                    playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                                    pending_playlist=pending_playlist,
                                    pending_captions=pending_captions,
                                    pending_publish=pending_publish,
                                    )
                                return EXIT_RETRY_LATER
                        video_id = final_channel_index.get(title)
                        if video_id:
                            break
                        logging.warning(
                            "影片尚未在 YouTube 播放清單或頻道中立即可讀；%s 秒後重試 (%s/%s): %s",
                            delay, attempt + 1, len(RECONCILIATION_DELAYS), title,
                        )
                        sleep_fn(delay)
                        try:
                            _refresh_playlist()
                        except UploadPaused as paused:
                            save_resume_state(
                                args.state_file, args.run_id, args.privacy, "paused",
                                paused.reason, paused.retry_at, completed_titles, part_plan,
                                pending_thumbnails,
                                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                                pending_playlist=pending_playlist,
                                pending_captions=pending_captions,
                                pending_publish=pending_publish,
                            )
                            return EXIT_RETRY_LATER
                        except Exception:
                            pass
                        if title in final_playlist_index:
                            video_id = final_playlist_index[title]
                            break
                        if known_video_id and any(item.get("video_id") == known_video_id for item in final_playlist_items):
                            video_id = known_video_id
                            final_playlist_index[title] = video_id
                            break
                        final_channel_index = None

                if not video_id:
                    raise RuntimeError(f"final reconciliation cannot find the video in either the playlist or channel uploads: {title}")

            # Verify playlist membership; wait for eventual consistency if not yet readable
            if title not in final_playlist_index and not any(item.get("video_id") == video_id for item in final_playlist_items):
                for attempt, delay in enumerate(RECONCILIATION_DELAYS):
                    logging.warning(
                        "Part %s (%s) 尚未在 YouTube 播放清單中可讀；%s 秒後重新讀取 (%s/%s)...",
                        part_num, title, delay, attempt + 1, len(RECONCILIATION_DELAYS),
                    )
                    sleep_fn(delay)
                    try:
                        _refresh_playlist()
                    except UploadPaused as paused:
                        save_resume_state(
                            args.state_file, args.run_id, args.privacy, "paused",
                            paused.reason, paused.retry_at, completed_titles, part_plan,
                            pending_thumbnails,
                            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                            pending_playlist=pending_playlist,
                            pending_captions=pending_captions,
                            pending_publish=pending_publish,
                        )
                        return EXIT_RETRY_LATER
                    except Exception:
                        pass
                    if title in final_playlist_index or any(item.get("video_id") == video_id for item in final_playlist_items):
                        break

            in_playlist = (
                title in final_playlist_index
                or any(item.get("video_id") == video_id for item in final_playlist_items)
            )
            if not in_playlist:
                if has_playlist_ack:
                    logging.warning(
                        "Part %s 已具備 successful playlist ACK，但在 playlistItems.list 中暫未可見；依 eventual consistency 保留 write ACK，絕不重複 insert: %s",
                        part_num, title,
                    )
                else:
                    logging.warning(
                        "Part %s 在播放清單中不可見且無 playlist ACK: %s",
                        part_num, title,
                    )

            evidence = {
                "youtube_video_id": video_id,
                "playlist_id": playlist_id,
                "playlist_position": int(planned["part_num"]) - 1,
                "verified_by": "final_playlist_audit",
            }
            steps = record.get("steps") or {}
            # Remote evidence only recovers what it actually proves
            if (steps.get("upload_video") or {}).get("status") != "completed":
                publication.complete(
                    part_num,
                    "upload_video",
                    recovered_from_youtube=True,
                    youtube_video_id=video_id,
                )
            if (steps.get("add_playlist") or {}).get("status") != "completed":
                if in_playlist or has_playlist_ack:
                    publication.complete(
                        part_num,
                        "add_playlist",
                        recovered_from_playlist=True,
                        youtube_video_id=video_id,
                        playlist_id=playlist_id,
                        playlist_position=int(planned["part_num"]) - 1,
                    )
            publication.complete(part_num, "final_validation", **evidence)

        if len(final_playlist_items) != len(part_plan):
            for attempt, delay in enumerate(RECONCILIATION_DELAYS):
                logging.warning(
                    "播放清單影片數 (%s/%s) 尚未同步完整；%s 秒後重新讀取 (%s/%s)...",
                    len(final_playlist_items), len(part_plan), delay, attempt + 1, len(RECONCILIATION_DELAYS),
                )
                sleep_fn(delay)
                try:
                    _refresh_playlist()
                except Exception:
                    pass
                if len(final_playlist_items) == len(part_plan):
                    break

        final_playlist_validation = validate_user_facing_playlist(
            final_playlist_items,
            part_plan,
        )
        hf_evidence = hf_archiver.verify_book(book_title, len(part_plan))
        for planned in part_plan:
            part_num = int(planned["part_num"])
            record = publication.data.get("parts", {}).get(str(part_num), {})
            if ((record.get("steps") or {}).get("archive_hf") or {}).get("status") != "completed":
                if part_num in hf_archiver.completed_parts(book_title):
                    publication.complete(part_num, "archive_hf", recovered_from_hf=True, hf_repo=hf_repo)
                else:
                    raise RuntimeError(f"Part {part_num} is missing mandatory Hugging Face archive evidence")
        logging.info(
            "[HF_ARCHIVE_STATUS] COMPLETE | archived=%s | total=%s | repo=%s | folder=%s",
            hf_evidence["parts"], len(part_plan), hf_evidence["repo_id"], hf_evidence["book_root"],
        )

    if final_playlist_validation is None:
        final_playlist_items = get_ordered_playlist_items(youtube, playlist_id)
        final_playlist_validation = validate_user_facing_playlist(
            final_playlist_items,
            part_plan,
        )

    measured_duration_seconds = sum(
        float(part.get("duration") or 0) for part in part_plan
    )
    if not part_plan or measured_duration_seconds <= 0:
        raise RuntimeError("缺少全部影片的實測時長，禁止填寫播放清單正式標題")
    final_playlist_title = completed_playlist_title(
        book_title, measured_duration_seconds,
    )
    try:
        update_playlist_metadata(
            youtube, playlist_id, final_playlist_title, playlist_desc,
        )
    except UploadPaused as paused:
        publication.mark_global("playlist", "paused", error=paused.reason)
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "paused",
            reason=paused.reason, retry_at=paused.retry_at,
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
            final_playlist_validation=final_playlist_validation,
        )
        logging.error(
            "[API_UPLOAD_STATUS] PAUSED during final playlist metadata update | "
            "retry_at=%s | source_run=%s | reason=%s",
            paused.retry_at.isoformat(), args.run_id, paused.reason,
        )
        return EXIT_RETRY_LATER
    final_playlist_validation["title"] = final_playlist_title
    final_playlist_validation["total_duration_seconds"] = measured_duration_seconds

    logging.info("="*60)
    logging.info(f"🎉 全部影片極速上傳完畢！共上傳 {total_uploaded} 部分部影片至 YouTube 播放清單！")
    if playlist_id:
        logging.info(f"👉 播放清單網址: https://www.youtube.com/playlist?list={playlist_id}")
    logging.info("="*60)
    publication.mark_global(
        "final_book_validation", "completed",
        completed_parts=len(completed_titles),
        playlist_items=final_playlist_validation["item_count"],
        ordered_parts=True,
    )
    save_resume_state(args.state_file, args.run_id, args.privacy, "complete",
                      completed_titles=completed_titles, part_plan=part_plan,
                      pending_thumbnails=pending_thumbnails,
                      pending_playlist=pending_playlist,
                      pending_captions=pending_captions,
                      pending_publish=pending_publish,
                      playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else None,
                      final_playlist_validation=final_playlist_validation)
    hf_executor.shutdown(wait=True)
    logging.info(
        "[API_UPLOAD_STATUS] COMPLETE | uploaded=%s | total=%s | source_run=%s",
        len(completed_titles), len(part_plan) or len(completed_titles), args.run_id or "local",
    )
    return 0
