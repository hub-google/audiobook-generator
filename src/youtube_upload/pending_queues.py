"""Drain and reconcile pending retry queues (thumbnails, captions, playlist, publish)."""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from .errors import UploadPaused, VideoNotFoundError
from .media import (
    resolve_part_cover,
    resolve_part_srt,
    set_video_privacy,
    set_video_thumbnail,
    upload_caption_file,
)
from .metadata import part_number_for_title
from .playlists import add_video_to_playlist
from .service_pool import EXIT_RETRY_LATER
from .state import save_resume_state
from .verification import verify_published_part

try:
    from ..metadata_gen import save_book_metadata
except ImportError:
    from metadata_gen import save_book_metadata


def drain_pending_thumbnails(youtube, publication, args, playlist_id,
                             completed_titles, part_plan, pending_thumbnails,
                             pending_playlist, pending_captions, pending_publish,
                             book_title):
    for pending_title, pending_video_id in list(pending_thumbnails.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        part_rec = publication.data.get("parts", {}).get(str(pending_part_num or 0), {})
        thumb_step = (part_rec.get("steps") or {}).get("upload_thumbnail") or {}
        if thumb_step.get("status") == "completed":
            logging.info("⏭️ Part %s 封面已於 Checkpoint 標記完成；清除待補佇列：%s", pending_part_num, pending_title)
            del pending_thumbnails[pending_title]
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "running",
                completed_titles=completed_titles, part_plan=part_plan,
                pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            continue

        cover_path = resolve_part_cover(title=pending_title, part_num=pending_part_num)
        if not cover_path and pending_part_num:
            for p in part_plan or []:
                if int(p.get("part_num", -1)) == int(pending_part_num):
                    s_c = int(p.get("start_chap", 1))
                    e_c = int(p.get("end_chap", s_c))
                    p_meta = save_book_metadata(book_title, s_c, e_c, is_completed=True, part_num=pending_part_num)
                    cover_path = p_meta.get("cover_file")
                    break

        if not cover_path or not os.path.exists(cover_path):
            logging.warning("⚠️ 找不到待補縮圖的封面檔案：%s (%s)，略過", pending_title, pending_video_id)
            del pending_thumbnails[pending_title]
            continue

        logging.info("🖼️ 正在補上傳 Part %s 封面：%s -> Video ID: %s", pending_part_num, cover_path, pending_video_id)
        try:
            set_video_thumbnail(youtube, pending_video_id, cover_path)
            if pending_part_num:
                publication.record_thumbnail_ack(pending_part_num)
            del pending_thumbnails[pending_title]
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "running",
                completed_titles=completed_titles, part_plan=part_plan,
                pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
        except UploadPaused as paused:
            if pending_part_num:
                publication.fail(pending_part_num, "upload_thumbnail", paused, paused=True, youtube_video_id=pending_video_id)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Thumbnail upload quota exhausted / rate-limited; retry after %s", paused.retry_at.isoformat())
            return EXIT_RETRY_LATER
    return None


def drain_pending_captions(youtube, publication, args, playlist_id,
                           completed_titles, existing_titles, part_plan,
                           pending_thumbnails, pending_playlist,
                           pending_captions, pending_publish):
    for pending_title, caption in list(pending_captions.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        video_id = caption.get("video_id")
        srt_path = caption.get("srt_path")
        try:
            caption_uploaded = upload_caption_file(youtube, video_id, srt_path)
        except VideoNotFoundError:
            logging.error(
                "❌ 待上傳字幕的影片已從 YouTube 消失 (Video ID: %s, Title: %s)。"
                "正在清除無效斷點紀錄，將重新進行上傳...",
                video_id, pending_title,
            )
            del pending_captions[pending_title]
            pending_playlist.pop(pending_title, None)
            pending_thumbnails.pop(pending_title, None)
            pending_publish.pop(pending_title, None)
            completed_titles.discard(pending_title)
            existing_titles.discard(pending_title)
            if pending_part_num:
                publication.reset_upload(pending_part_num, reason=f"videoNotFound:{video_id}")
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
            if pending_part_num:
                publication.fail(pending_part_num, "upload_caption", paused, paused=True, youtube_video_id=video_id)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Caption quota exhausted; retry after %s", paused.retry_at.isoformat())
            return EXIT_RETRY_LATER
        if not caption_uploaded:
            if pending_part_num:
                publication.fail(pending_part_num, "upload_caption", RuntimeError("caption upload failed"), paused=True, youtube_video_id=video_id)
            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                "captionUploadFailed", retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            return EXIT_RETRY_LATER
        if pending_part_num:
            publication.complete(pending_part_num, "upload_caption", youtube_video_id=video_id)
    return None


def drain_pending_playlist(youtube, publication, args, playlist_id,
                           completed_titles, existing_titles, part_plan,
                           pending_thumbnails, pending_playlist,
                           pending_captions, pending_publish):
    for pending_title, pending_video_id in list(pending_playlist.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        planned = next((p for p in part_plan if p.get("title") == pending_title), {})
        position = int(planned.get("part_num", 0)) - 1 if planned else None
        try:
            added = add_video_to_playlist(youtube, playlist_id, pending_video_id, position)
        except VideoNotFoundError:
            logging.error(
                "❌ 待加入播放清單的影片已從 YouTube 消失 (Video ID: %s, Title: %s)。"
                "正在清除無效斷點紀錄，將重新進行上傳...",
                pending_video_id, pending_title,
            )
            del pending_playlist[pending_title]
            pending_thumbnails.pop(pending_title, None)
            pending_captions.pop(pending_title, None)
            pending_publish.pop(pending_title, None)
            completed_titles.discard(pending_title)
            existing_titles.discard(pending_title)
            if pending_part_num:
                publication.reset_upload(pending_part_num, reason=f"videoNotFound:{pending_video_id}")
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
            if pending_part_num:
                publication.fail(pending_part_num, "add_playlist", paused, paused=True, youtube_video_id=pending_video_id)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Playlist insertion quota exhausted: %s; retry after %s", pending_title, paused.retry_at.isoformat())
            return EXIT_RETRY_LATER
        if not added:
            if pending_part_num:
                publication.fail(pending_part_num, "add_playlist", RuntimeError("playlist insertion failed"), paused=True, youtube_video_id=pending_video_id)
            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                "playlistInsertFailed", retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Playlist insertion is mandatory; Part remains incomplete: %s", pending_title)
            return EXIT_RETRY_LATER
        del pending_playlist[pending_title]
        if pending_part_num:
            publication.record_playlist_ack(pending_part_num, added, position)
            publication.complete(pending_part_num, "final_validation", deferred_to_final_audit=True)
        completed_titles.add(pending_title)
        existing_titles.add(pending_title)
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "running",
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
    return None


def drain_pending_publish(youtube, publication, args, playlist_id,
                          completed_titles, existing_titles, part_plan,
                          pending_thumbnails, pending_playlist,
                          pending_captions, pending_publish):
    for pending_title, pending_video_id in list(pending_publish.items()):
        pending_part_num = part_number_for_title(part_plan, pending_title)
        pending_srt_path = resolve_part_srt(
            title=pending_title,
            part_num=pending_part_num,
        )
        if pending_srt_path:
            pending_captions[pending_title] = {
                "video_id": pending_video_id,
                "srt_path": pending_srt_path,
            }
        try:
            published = set_video_privacy(youtube, pending_video_id, args.privacy)
        except VideoNotFoundError:
            logging.error(
                "❌ 待發布的影片已從 YouTube 消失 (Video ID: %s, Title: %s)。"
                "正在清除無效斷點紀錄，將重新進行上傳...",
                pending_video_id, pending_title,
            )
            del pending_publish[pending_title]
            pending_playlist.pop(pending_title, None)
            pending_thumbnails.pop(pending_title, None)
            pending_captions.pop(pending_title, None)
            completed_titles.discard(pending_title)
            existing_titles.discard(pending_title)
            if pending_part_num:
                publication.reset_upload(pending_part_num, reason=f"videoNotFound:{pending_video_id}")
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
            if pending_part_num:
                publication.fail(pending_part_num, "publish", paused, paused=True, youtube_video_id=pending_video_id)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                paused.reason, paused.retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            logging.error("Video publish quota exhausted: %s; retry after %s", pending_title, paused.retry_at.isoformat())
            return EXIT_RETRY_LATER
        if not published:
            if pending_part_num:
                publication.fail(pending_part_num, "publish", RuntimeError("final publish failed"), paused=True, youtube_video_id=pending_video_id)
            retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                "publishFailed", retry_at, completed_titles, part_plan,
                pending_thumbnails,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
            )
            return EXIT_RETRY_LATER
        del pending_publish[pending_title]
        completed_titles.add(pending_title)
        if pending_part_num:
            publication.complete(pending_part_num, "publish", youtube_video_id=pending_video_id, privacy=args.privacy)
            publication.mark(pending_part_num, "final_validation", "running")
            try:
                evidence = verify_published_part(
                    youtube, pending_video_id, playlist_id, args.privacy,
                    srt_path=pending_srt_path,
                    part_title=pending_title,
                    part_num=pending_part_num,
                    playlist_position=pending_part_num - 1 if pending_part_num else None,
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
                logging.error("Validation quota exhausted: %s; retry after %s", pending_title, paused.retry_at.isoformat())
                return EXIT_RETRY_LATER
            publication.complete(pending_part_num, "final_validation", **evidence)
            pending_captions.pop(pending_title, None)
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "running",
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
    return None
