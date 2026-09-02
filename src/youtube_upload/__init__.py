"""Reusable components for the YouTube publication pipeline."""

from .ci_pipeline import run_ci_artifact_mode
from .errors import (
    UploadPaused,
    VideoNotFoundError,
    classify_daily_limit,
    is_transient_upload_error,
    is_transient_youtube_api_error,
)
from .final_audit import run_final_playlist_and_archive_audit
from .local_pipeline import run_local_prepared_parts_mode
from .media import (
    THUMBNAIL_MIN_INTERVAL_SECONDS,
    _file_sha256,
    _last_thumbnail_request_at,
    generate_part_srt,
    is_valid_chinese_caption,
    resolve_part_cover,
    resolve_part_srt,
    set_video_privacy,
    set_video_thumbnail,
    upload_caption_file,
    upload_video_file,
)
from .metadata import (
    _chapter_title,
    build_chapter_timeline,
    build_video_description,
    part_number_for_title,
)
from .orchestrator import run_upload_pipeline
from .pending_queues import (
    drain_pending_captions,
    drain_pending_playlist,
    drain_pending_publish,
    drain_pending_thumbnails,
)
from .planning import (
    _find_gh,
    _make_planned_part,
    _validate_complete_chapter_inventory,
    artifact_worker_index,
    build_part_plan_from_inventory,
    download_artifact_task,
    get_latest_successful_run_id,
    get_run_artifact_names,
    get_run_manifest_artifact_names,
    parse_chapter_info,
    scan_artifact_chapters,
    select_manifest_artifacts,
    select_worker_artifacts,
    validate_chapter_inventory,
)
from .playlists import (
    add_video_to_playlist,
    completed_playlist_title,
    get_channel_upload_video_index,
    get_existing_playlist_video_titles,
    get_or_create_playlist,
    get_ordered_playlist_items,
    get_playlist_video_index,
    load_measured_prepared_part_plan,
    update_playlist_metadata,
    validate_user_facing_playlist,
)
from .service_pool import (
    EXIT_RETRY_LATER,
    SCOPES,
    YOUTUBE_SLOT_ROTATION_ROUNDS,
    YouTubeServicePool,
    get_authenticated_service,
)
from .state import (
    MAX_YOUTUBE_ACCOUNT_SLOTS,
    atomic_write_json,
    configured_youtube_account_slots,
    load_resume_state,
    recover_completed_titles_from_playlist,
    save_resume_state,
)
from .verification import verify_published_part

__all__ = [
    # errors
    "UploadPaused",
    "VideoNotFoundError",
    "classify_daily_limit",
    "is_transient_upload_error",
    "is_transient_youtube_api_error",
    # state
    "MAX_YOUTUBE_ACCOUNT_SLOTS",
    "atomic_write_json",
    "configured_youtube_account_slots",
    "load_resume_state",
    "recover_completed_titles_from_playlist",
    "save_resume_state",
    # metadata
    "_chapter_title",
    "build_chapter_timeline",
    "build_video_description",
    "part_number_for_title",
    # service_pool
    "EXIT_RETRY_LATER",
    "SCOPES",
    "YOUTUBE_SLOT_ROTATION_ROUNDS",
    "YouTubeServicePool",
    "get_authenticated_service",
    # playlists
    "add_video_to_playlist",
    "completed_playlist_title",
    "get_channel_upload_video_index",
    "get_existing_playlist_video_titles",
    "get_or_create_playlist",
    "get_ordered_playlist_items",
    "get_playlist_video_index",
    "load_measured_prepared_part_plan",
    "update_playlist_metadata",
    "validate_user_facing_playlist",
    # media
    "THUMBNAIL_MIN_INTERVAL_SECONDS",
    "_file_sha256",
    "_last_thumbnail_request_at",
    "generate_part_srt",
    "is_valid_chinese_caption",
    "resolve_part_cover",
    "resolve_part_srt",
    "set_video_privacy",
    "set_video_thumbnail",
    "upload_caption_file",
    "upload_video_file",
    # planning
    "_find_gh",
    "_make_planned_part",
    "_validate_complete_chapter_inventory",
    "artifact_worker_index",
    "build_part_plan_from_inventory",
    "download_artifact_task",
    "get_latest_successful_run_id",
    "get_run_artifact_names",
    "get_run_manifest_artifact_names",
    "parse_chapter_info",
    "scan_artifact_chapters",
    "select_manifest_artifacts",
    "select_worker_artifacts",
    "validate_chapter_inventory",
    # verification
    "verify_published_part",
    # pipeline stages
    "drain_pending_thumbnails",
    "drain_pending_captions",
    "drain_pending_playlist",
    "drain_pending_publish",
    "run_ci_artifact_mode",
    "run_local_prepared_parts_mode",
    "run_final_playlist_and_archive_audit",
    # orchestrator
    "run_upload_pipeline",
]
