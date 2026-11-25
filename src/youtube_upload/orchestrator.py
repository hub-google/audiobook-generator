"""YouTube upload pipeline orchestrator and lifecycle coordinator."""

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from .ci_pipeline import run_ci_artifact_mode
from .errors import UploadPaused
from .final_audit import run_final_playlist_and_archive_audit
from .local_pipeline import run_local_prepared_parts_mode
from .metadata import part_number_for_title
from .pending_queues import (
    drain_pending_captions,
    drain_pending_playlist,
    drain_pending_publish,
    drain_pending_thumbnails,
)
from .planning import download_artifact_task, get_latest_successful_run_id
from .playlists import (
    completed_playlist_title,
    get_channel_upload_video_index,
    get_or_create_playlist,
    get_playlist_video_index,
    load_measured_prepared_part_plan,
    update_playlist_metadata,
)
from .service_pool import (
    EXIT_RETRY_LATER,
    YouTubeServicePool,
    get_authenticated_service,
)
from .state import (
    configured_youtube_account_slots,
    load_resume_state,
    save_resume_state,
)

try:
    from ..huggingface_archiver import HuggingFaceArchiver
    from ..metadata_gen import save_book_metadata
    from ..publication_checkpoint import PublicationCheckpoint, plan_fingerprint
except ImportError:
    from huggingface_archiver import HuggingFaceArchiver
    from metadata_gen import save_book_metadata
    from publication_checkpoint import PublicationCheckpoint, plan_fingerprint


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def run_upload_pipeline(args):
    """Main pipeline execution logic for YouTube video publishing."""
    if getattr(args, "auth_pool", False):
        pool = YouTubeServicePool()
        pool.authorize_all_local(sync_github=not getattr(args, "no_sync_gh", False))
        return 0

    publication = PublicationCheckpoint(args.state_file)

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        raise RuntimeError("HF_TOKEN is required because the production archive is mandatory")
    hf_repo = os.environ.get("HF_ARCHIVE_REPO", "").strip()

    if not hf_repo:
        from huggingface_hub import HfApi
        hf_repo = f"{HfApi(token=hf_token).whoami()['name']}/audiobook-archive"
    hf_archiver = HuggingFaceArchiver(
        hf_repo, hf_token,
        os.path.join(os.path.dirname(os.path.abspath(args.state_file)), "hf_archive_state.json"),
    )
    hf_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf-archive")

    source_run_id = getattr(args, "source_run_id", None) or getattr(args, "run_id", "") or ""
    execution_run_id = getattr(args, "execution_run_id", None) or os.environ.get("GITHUB_RUN_ID", "") or getattr(args, "run_id", "") or ""

    saved_state = load_resume_state(args.state_file)
    completed_titles = set()
    part_plan = []
    pending_thumbnails = {}
    pending_playlist = {}
    pending_captions = {}
    pending_publish = {}
    resume_state_matches = False
    if saved_state:
        # Check task identity:
        # 1. task_id check
        if args.task_id and saved_state.get("task_id"):
            if str(saved_state.get("task_id")).strip() != str(args.task_id).strip():
                raise RuntimeError(
                    f"Checkpoint task mismatch: expected {args.task_id!r}, found {saved_state.get('task_id')!r}; refusing foreign checkpoint"
                )
        # 2. publication ledger identity check
        if publication.is_locked():
            valid, reason = publication.validate_task_identity(task_id=args.task_id)
            if not valid:
                raise RuntimeError(f"Publication ledger task mismatch: {reason}; refusing foreign checkpoint")

        resume_state_matches = True
        completed_titles.update(saved_state.get("completed_titles") or [])
        part_plan = list(saved_state.get("part_plan") or [])
        pending_thumbnails = dict(saved_state.get("pending_thumbnails") or {})
        pending_playlist = dict(saved_state.get("pending_playlist") or {})
        pending_captions = dict(saved_state.get("pending_captions") or {})
        pending_publish = dict(saved_state.get("pending_publish") or {})

        # Inherit source_run_id from previous checkpoint if available
        if not getattr(args, "source_run_id", None) and (saved_state.get("source_run_id") or saved_state.get("run_id")):
            source_run_id = saved_state.get("source_run_id") or saved_state.get("run_id")

    valid_resume_statuses = {"paused", "running", "planned", "incomplete"}
    if resume_state_matches and saved_state.get("status") in valid_resume_statuses:
        if not args.run_id:
            args.run_id = str(saved_state.get("run_id") or "")
            args.privacy = saved_state.get("privacy") or args.privacy
        retry_text = saved_state.get("retry_at")
        if saved_state.get("status") == "paused" and retry_text:
            retry_at = datetime.fromisoformat(retry_text.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < retry_at:
                saved_pool_size = int(saved_state.get("credential_pool_size") or 1)
                current_pool_size = len(configured_youtube_account_slots())
                if saved_state.get("reason") == "thumbnailRateLimit":
                    logging.info(
                        "⏭️ 自訂縮圖已停用；忽略舊 thumbnailRateLimit 冷卻並繼續發布。"
                    )
                elif (
                    saved_state.get("reason") == "quotaExceeded"
                    and current_pool_size > saved_pool_size
                ):
                    logging.info(
                        "🔄 YouTube 憑證池可用 %s 組（舊斷點記錄 %s 組）；"
                        "忽略舊 API 專案配額等待時間並立即從下一個 API slot 繼續。",
                        current_pool_size,
                        saved_pool_size,
                    )
                elif os.environ.get("MANUAL_YOUTUBE_RETRY", "").lower() == "true":
                    logging.warning(
                        "⚠️ 偵測到使用者手動 Re-run；忽略尚未到期的安全重試時間 %s，"
                        "現在將實際測試 YouTube API 憑證池。",
                        retry_at.isoformat(),
                    )
                else:
                    logging.info("⏳ 尚未到安全重試時間 %s；本次排程不會呼叫 YouTube。", retry_at.isoformat())
                    return EXIT_RETRY_LATER

    if not args.run_id and not args.input_dir:
        logging.info("🔍 未指定 --run-id 且無有效斷點紀錄，嘗試自動查詢最新產檔成功的 Run ID...")
        latest_run_id = get_latest_successful_run_id(args.repo)
        if latest_run_id:
            args.run_id = latest_run_id
            logging.info("💡 自動鎖定最新產檔 Run ID: %s", args.run_id)
        else:
            logging.error("Strict success gate: no source run or local input was found")
            return 1

    try:
        youtube = get_authenticated_service()
    except UploadPaused as paused:
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "paused",
            reason=paused.reason, retry_at=paused.retry_at,
            completed_titles=completed_titles, part_plan=part_plan,
            pending_thumbnails=pending_thumbnails,
            pending_playlist=pending_playlist,
            pending_captions=pending_captions,
            pending_publish=pending_publish,
        )
        logging.error(
            "[API_UPLOAD_STATUS] PAUSED during credential validation | "
            "retry_at=%s | source_run=%s | reason=%s",
            paused.retry_at.isoformat(), args.run_id, paused.reason,
        )
        return EXIT_RETRY_LATER

    SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    book_title = "有聲小說全集"
    book_profile_id = getattr(args, "book_profile_id", "") or ""
    source_fingerprint = ""
    start_chap, end_chap = 1, 2400
    config_path = os.path.join(SRC_DIR, "..", "config.yaml")
    if args.input_dir and os.path.exists(os.path.join(args.input_dir, "config.yaml")):
        config_path = os.path.join(args.input_dir, "config.yaml")
        logging.info("已載入 prepared Parts 內鎖定的來源 config：%s", config_path)
    elif args.run_id:
        shared_config_dir = os.path.abspath("temp_source_run_config")
        if download_artifact_task(args.run_id, args.repo, "shared-config", shared_config_dir):
            downloaded_config = os.path.join(shared_config_dir, "config.yaml")
            if os.path.exists(downloaded_config):
                config_path = downloaded_config
                logging.info("已載入來源 Run 的 shared-config：%s", config_path)
        else:
            raise RuntimeError(
                f"Strict success gate: source Run #{args.run_id} has no downloadable shared-config artifact"
            )
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if cfg:
                    book_title = cfg.get("book_title", book_title)
                    book_profile_id = cfg.get("book_profile_id", book_profile_id)
                    source_fingerprint = cfg.get("source_fingerprint", "")
                    hf_archiver.source_fingerprint = source_fingerprint
                    os.environ['BOOK_SOURCE_FINGERPRINT'] = source_fingerprint
                    os.environ['BOOK_CATALOG_URL'] = cfg.get('catalog_url', '')
                    os.environ['BOOK_PREPARED_DIR'] = os.path.abspath(args.input_dir) if args.input_dir else ''
                    chaps = cfg.get("selected_indices", [])
                    if chaps:
                        start_chap = chaps[0]
                        end_chap = chaps[-1]
        except Exception as e:
            logging.warning(f"Could not load config.yaml: {e}")

    meta_info = save_book_metadata(book_title, start_chap, end_chap)
    cover_path = meta_info["cover_file"]
    if args.input_dir:
        measured_plan = load_measured_prepared_part_plan(args.input_dir, book_title)
        if measured_plan:
            measured_durations = {
                int(p["part_num"]): float(p.get("duration") or 0)
                for p in measured_plan
            }
            if any(dur <= 0 for dur in measured_durations.values()):
                raise RuntimeError("缺少全部影片的實測時長，任一 Part 時長必須大於 0")

            if part_plan:
                locked_part_nums = {int(p["part_num"]) for p in part_plan}
                measured_part_nums = set(measured_durations.keys())
                if locked_part_nums != measured_part_nums:
                    raise RuntimeError(
                        f"Measured parts {sorted(measured_part_nums)} do not match locked plan parts {sorted(locked_part_nums)}"
                    )
                runtime_plan = [dict(p) for p in part_plan]
                for p in runtime_plan:
                    p["duration"] = measured_durations[int(p["part_num"])]
                part_plan = runtime_plan
            else:
                part_plan = measured_plan
            logging.info("✅ 已在建立播放清單前實測 %s 個 prepared Part 的 MP4 時長。", len(part_plan))
    if saved_state and saved_state.get("book_profile_id") and book_profile_id:
        if str(saved_state["book_profile_id"]).strip() != str(book_profile_id).strip():
            raise RuntimeError(
                f"Checkpoint book_profile_id mismatch: expected {book_profile_id!r}, found {saved_state['book_profile_id']!r}; refusing foreign checkpoint"
            )
    if publication.is_locked():
        valid, reason = publication.validate_task_identity(book_profile_id=book_profile_id, part_plan=part_plan)
        if not valid:
            raise RuntimeError(f"Publication ledger mismatch: {reason}; refusing foreign checkpoint")
    if part_plan:
        publication.lock_plan(
            part_plan,
            run_id=source_run_id,
            book_title=book_title,
            book_profile_id=book_profile_id,
            task_id=args.task_id,
            execution_run_id=execution_run_id,
        )

    if source_fingerprint:
        if publication.data.get('source_fingerprint') not in (None, '', source_fingerprint):
            raise RuntimeError('Publication source fingerprint mismatch')
        publication.data['source_fingerprint'] = source_fingerprint
        publication.save()

    measured_duration_seconds = sum(
        float(part.get("duration") or 0) for part in (part_plan or [])
    )
    if not part_plan or any(float(part.get("duration") or 0) <= 0 for part in part_plan):
        raise RuntimeError("缺少全部影片的實測時長，禁止建立播放清單")
    playlist_name = completed_playlist_title(book_title, measured_duration_seconds)
    legacy_playlist_name = f"《{book_title}》有聲小說全集"
    processing_playlist_name = f"[處理中]《{book_title}》全集"
    resumable_playlist_titles = [legacy_playlist_name, processing_playlist_name]
    playlist_desc = f"《{book_title}》完整版有聲書全集 (第 {start_chap} 至 {end_chap} 章)，高音質連續播映版。\n歡迎訂閱開啟小鈴鐺！"
    if source_fingerprint:
        playlist_desc += f"\n來源識別：{source_fingerprint}"
    publication.mark_global("playlist", "running")
    try:
        playlist_id, playlist_created = get_or_create_playlist(
            youtube, playlist_name, playlist_desc,
            alternate_titles=resumable_playlist_titles,
            **({"source_fingerprint": source_fingerprint} if source_fingerprint else {}),
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
        )
        logging.error(
            "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
            len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
            args.run_id, paused.reason,
        )
        return EXIT_RETRY_LATER
    if not playlist_id:
        publication.mark_global("playlist", "failed", error="playlistUnavailable")
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "failed",
            reason="playlistUnavailable", completed_titles=completed_titles,
            part_plan=part_plan, pending_thumbnails=pending_thumbnails,
        )
        logging.error("無法取得或建立 YouTube 播放清單，停止上傳。")
        return 1

    try:
        update_playlist_metadata(youtube, playlist_id, playlist_name, playlist_desc)
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
        )
        logging.error(
            "[API_UPLOAD_STATUS] PAUSED during playlist metadata update | "
            "retry_at=%s | source_run=%s | reason=%s",
            paused.retry_at.isoformat(), args.run_id, paused.reason,
        )
        return EXIT_RETRY_LATER
    save_resume_state(
        args.state_file, args.run_id, args.privacy, "running",
        completed_titles=completed_titles, part_plan=part_plan,
        pending_thumbnails=pending_thumbnails,
        pending_playlist=pending_playlist,
        pending_captions=pending_captions,
        pending_publish=pending_publish,
        playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
    )
    publication.mark_global("playlist", "completed", playlist_id=playlist_id)
    try:
        existing_video_ids = (
            {} if playlist_created else get_playlist_video_index(youtube, playlist_id)
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
        )
        logging.error(
            "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
            len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
            args.run_id, paused.reason,
        )
        return EXIT_RETRY_LATER
    except Exception as error:
        save_resume_state(
            args.state_file, args.run_id, args.privacy, "failed",
            reason="playlistReadFailed", completed_titles=completed_titles,
            part_plan=part_plan, pending_thumbnails=pending_thumbnails,
            playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        )
        logging.error("播放清單重試後仍無法讀取：%s", error)
        return 1
    existing_titles = set(existing_video_ids)
    logging.info("📋 成功獲取播放清單已有 %s 部影片。", len(existing_titles))

    missing_from_playlist = completed_titles - existing_titles
    if missing_from_playlist:
        try:
            channel_uploads = get_channel_upload_video_index(youtube)
        except UploadPaused as paused:
            save_resume_state(
                args.state_file, args.run_id, args.privacy, "paused",
                reason=paused.reason, retry_at=paused.retry_at,
                completed_titles=completed_titles, part_plan=part_plan,
                pending_thumbnails=pending_thumbnails,
                pending_playlist=pending_playlist,
                pending_captions=pending_captions,
                pending_publish=pending_publish,
                playlist_url=f"https://www.youtube.com/playlist?list={playlist_id}",
            )
            logging.error(
                "[API_UPLOAD_STATUS] PAUSED | uploaded=%s | total=%s | retry_at=%s | source_run=%s | reason=%s",
                len(completed_titles), len(part_plan), paused.retry_at.isoformat(),
                args.run_id, paused.reason,
            )
            return EXIT_RETRY_LATER
        for title in list(missing_from_playlist):
            video_id = channel_uploads.get(title)
            if not video_id:
                logging.error("Uploaded title is missing from the playlist and channel uploads: %s", title)
                completed_titles.discard(title)
                pending_part_num = part_number_for_title(part_plan, title)
                if pending_part_num:
                    publication.reset_upload(pending_part_num, reason="missing_from_playlist_and_channel")
                continue
            pending_playlist[title] = video_id
            completed_titles.discard(title)
            logging.warning("Repairing orphan video playlist membership: %s (%s)", title, video_id)

    # 1. Drain pending thumbnail updates
    exit_code = drain_pending_thumbnails(
        youtube, publication, args, playlist_id,
        completed_titles, part_plan, pending_thumbnails,
        pending_playlist, pending_captions, pending_publish,
        book_title, existing_titles=existing_titles,
    )
    if exit_code is not None:
        return exit_code

    # 2. Drain pending caption uploads
    exit_code = drain_pending_captions(
        youtube, publication, args, playlist_id,
        completed_titles, existing_titles, part_plan,
        pending_thumbnails, pending_playlist,
        pending_captions, pending_publish,
    )
    if exit_code is not None:
        return exit_code

    # 3. Drain pending playlist membership
    exit_code = drain_pending_playlist(
        youtube, publication, args, playlist_id,
        completed_titles, existing_titles, part_plan,
        pending_thumbnails, pending_playlist,
        pending_captions, pending_publish,
    )
    if exit_code is not None:
        return exit_code

    # 4. Drain pending publish requests
    exit_code = drain_pending_publish(
        youtube, publication, args, playlist_id,
        completed_titles, existing_titles, part_plan,
        pending_thumbnails, pending_playlist,
        pending_captions, pending_publish,
    )
    if exit_code is not None:
        return exit_code

    upload_subtitles_dir = os.path.abspath(os.path.join("Upload_Subtitles", source_fingerprint))
    os.makedirs(upload_subtitles_dir, exist_ok=True)
    temp_parts_dir = os.path.abspath(os.path.join("temp_parts_output", source_fingerprint))
    os.makedirs(temp_parts_dir, exist_ok=True)
    temp_dl_dir = os.path.abspath(os.path.join("temp_api_upload_workspace", source_fingerprint))
    os.makedirs(temp_dl_dir, exist_ok=True)

    total_uploaded = 0

    if args.run_id and not args.input_dir:
        part_plan, total_uploaded, exit_code = run_ci_artifact_mode(
            youtube, publication, hf_archiver, hf_executor, args, playlist_id,
            completed_titles, existing_titles, existing_video_ids, part_plan,
            pending_thumbnails, pending_playlist, pending_captions, pending_publish,
            book_title, start_chap, end_chap, config_path, hf_repo,
            temp_parts_dir, upload_subtitles_dir, temp_dl_dir,
        )
        if exit_code != 0:
            return exit_code

    elif args.input_dir and os.path.exists(args.input_dir):
        parts_to_upload, part_plan, total_uploaded, exit_code = run_local_prepared_parts_mode(
            youtube, publication, hf_archiver, hf_executor, args, playlist_id,
            completed_titles, existing_titles, existing_video_ids, part_plan,
            pending_thumbnails, pending_playlist, pending_captions, pending_publish,
            book_title, start_chap, end_chap, config_path, hf_repo,
            temp_parts_dir, upload_subtitles_dir,
        )
        if exit_code != 0:
            return exit_code

    return run_final_playlist_and_archive_audit(
        youtube, publication, hf_archiver, hf_executor, args, playlist_id,
        playlist_desc, completed_titles, part_plan, pending_thumbnails,
        pending_playlist, pending_captions, pending_publish, book_title,
        hf_repo, total_uploaded,
    )
