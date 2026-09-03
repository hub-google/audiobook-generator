"""
youtube_api_uploader.py — YouTube Data API v3 暴速影片上傳 + 自動播放清單建置工具

Workflow validation markers:
- PAUSED during playlist metadata update
- PAUSED during final playlist metadata update
"""

import argparse
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import HttpError, MediaFileUpload

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [YouTube-API] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

try:
    from .artifact_validation import validate_image, validate_srt, validate_video
    from .huggingface_archiver import HuggingFaceArchiver
    from .part_builder import (
        duration_from_srt,
        get_media_duration,
        merge_part_videos,
        parse_chapter_num,
    )
    from .publication_checkpoint import PART_STEPS, PublicationCheckpoint
    from .source_status import confirmed_missing_from_directory
    from .youtube_upload import (
        EXIT_RETRY_LATER,
        MAX_YOUTUBE_ACCOUNT_SLOTS,
        SCOPES,
        THUMBNAIL_MIN_INTERVAL_SECONDS,
        UploadPaused,
        VideoNotFoundError,
        YouTubeServicePool,
        YOUTUBE_SLOT_ROTATION_ROUNDS,
        _chapter_title,
        _file_sha256,
        _find_gh,
        _last_thumbnail_request_at,
        _make_planned_part,
        _validate_complete_chapter_inventory,
        add_video_to_playlist,
        artifact_worker_index,
        atomic_write_json,
        build_chapter_timeline,
        build_part_plan_from_inventory,
        build_video_description,
        classify_daily_limit,
        completed_playlist_title,
        configured_youtube_account_slots,
        download_artifact_task,
        generate_part_srt,
        get_authenticated_service,
        get_channel_upload_video_index,
        get_existing_playlist_video_titles,
        get_latest_successful_run_id,
        get_or_create_playlist,
        get_ordered_playlist_items,
        get_playlist_video_index,
        get_run_artifact_names,
        get_run_manifest_artifact_names,
        is_transient_upload_error,
        is_transient_youtube_api_error,
        is_valid_chinese_caption,
        load_measured_prepared_part_plan,
        load_resume_state,
        parse_chapter_info,
        part_number_for_title,
        recover_completed_titles_from_playlist,
        resolve_part_cover,
        resolve_part_srt,
        run_upload_pipeline,
        save_resume_state,
        scan_artifact_chapters,
        select_manifest_artifacts,
        select_worker_artifacts,
        set_video_privacy,
        set_video_thumbnail,
        update_playlist_metadata,
        upload_caption_file,
        upload_video_file,
        validate_chapter_inventory,
        validate_state_identity,
        validate_user_facing_playlist,
        verify_published_part,
    )
except ImportError:
    from artifact_validation import validate_image, validate_srt, validate_video
    from huggingface_archiver import HuggingFaceArchiver
    from part_builder import (
        duration_from_srt,
        get_media_duration,
        merge_part_videos,
        parse_chapter_num,
    )
    from publication_checkpoint import PART_STEPS, PublicationCheckpoint
    from source_status import confirmed_missing_from_directory
    from youtube_upload import (
        EXIT_RETRY_LATER,
        MAX_YOUTUBE_ACCOUNT_SLOTS,
        SCOPES,
        THUMBNAIL_MIN_INTERVAL_SECONDS,
        UploadPaused,
        VideoNotFoundError,
        YouTubeServicePool,
        YOUTUBE_SLOT_ROTATION_ROUNDS,
        _chapter_title,
        _file_sha256,
        _find_gh,
        _last_thumbnail_request_at,
        _make_planned_part,
        _validate_complete_chapter_inventory,
        add_video_to_playlist,
        artifact_worker_index,
        atomic_write_json,
        build_chapter_timeline,
        build_part_plan_from_inventory,
        build_video_description,
        classify_daily_limit,
        completed_playlist_title,
        configured_youtube_account_slots,
        download_artifact_task,
        generate_part_srt,
        get_authenticated_service,
        get_channel_upload_video_index,
        get_existing_playlist_video_titles,
        get_latest_successful_run_id,
        get_or_create_playlist,
        get_ordered_playlist_items,
        get_playlist_video_index,
        get_run_artifact_names,
        get_run_manifest_artifact_names,
        is_transient_upload_error,
        is_transient_youtube_api_error,
        is_valid_chinese_caption,
        load_measured_prepared_part_plan,
        load_resume_state,
        parse_chapter_info,
        part_number_for_title,
        recover_completed_titles_from_playlist,
        resolve_part_cover,
        resolve_part_srt,
        run_upload_pipeline,
        save_resume_state,
        scan_artifact_chapters,
        select_manifest_artifacts,
        select_worker_artifacts,
        set_video_privacy,
        set_video_thumbnail,
        update_playlist_metadata,
        upload_caption_file,
        upload_video_file,
        validate_chapter_inventory,
        validate_state_identity,
        validate_user_facing_playlist,
        verify_published_part,
    )

_atomic_write_json = atomic_write_json


def main():
    parser = argparse.ArgumentParser(description="YouTube API Fast Uploader & Playlist Builder")
    parser.add_argument("--run-id", help="GitHub Actions Run ID containing video worker artifacts")
    parser.add_argument("--source-run-id", help="Original source run ID for this publication task")
    parser.add_argument("--execution-run-id", help="Current execution run ID (retry / resume run ID)")
    parser.add_argument("--book-title", help="Book title override")
    parser.add_argument("--input-dir", help="Local directory containing MP4 files")
    parser.add_argument("--repo", default="hub-google/audiobook-generator", help="GitHub Repository")
    parser.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"], help="Privacy status")
    parser.add_argument("--state-file", default="upload_resume_state/state.json",
                        help="Durable state restored/saved by GitHub Actions")
    parser.add_argument("--task-id", default=os.environ.get("QUEUE_TASK_ID", ""), help="Persistent cloud queue task ID")
    parser.add_argument("--auth-pool", action="store_true", help="Authorize all project client_secrets and generate tokens locally")
    parser.add_argument("--no-sync-gh", action="store_true", help="Do not sync generated tokens to GitHub secrets")
    args = parser.parse_args()

    return run_upload_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
