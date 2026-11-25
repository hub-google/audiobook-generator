from urllib.parse import urljoin
import os
import sys
import json
import logging
import time
import shutil
import subprocess
import requests
import threading
import tkinter as tk
from decimal import Decimal, InvalidOperation
from tkinter import ttk, messagebox, scrolledtext, filedialog, simpledialog
from dotenv import load_dotenv
import re
import webbrowser
import base64
import io
import zipfile
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageTk
from .source_ui import (
    AUTO_SOURCE_LABEL, SOURCE_EXAMPLES, SOURCE_OPTIONS, SOURCE_NAMES,
    catalog_matches_current, detect_source, normalize_source_url,
    normalize_batch_urls, source_hint, source_id_from_label, source_label,
    SourceParseError,
)

# Use a single canonical import path. Importing the same business module once
# as "catalog_parser" and again as "src.catalog_parser" creates independent
# globals, mocks and caches, and makes the GUI difficult to reason about.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)

from src.catalog_parser import (
    analyze_duplicate_chapters, apply_chapter_title_overrides, fetch_69shuba_full_novels,
    find_direct_duplicate_matches, parse_catalog, split_chapter_title,
)
from src.chapter_numbers import normalize_positive_chapter_number
from src.cleaner import chunk_text, clean_text_content
from src.book_profiles import (
    GitHubBookProfileStore, book_profile_id, get_book_profile, profile_snapshot,
    update_book_profile, validate_remove_patterns,
)
from src.crawler import fetch_chapter_text
from src.cloud_queue import (
    BLOCKING_STATES, preflight_status, GitHubQueueStore, add_tasks, delete_task, approve_preflight,
    confirm_preflight_review, start_reviewed_processing,
    format_chapter_label, is_task_active, mark_task_completed, mark_task_interrupted,
    mark_task_waiting_retry, mark_tasks_completed, move_chapter_order, move_tasks,
    move_tasks_to_pending, new_task, normalize_chapter_order, requeue_task_after_active,
    retry_failed_preflight_stages, settle_interrupted_task, task_id_from_run_name,
    update_task, update_task_chapters,
)
from src.github_run_status import (
    error_observation, missing_observation, observation_text, successful_observation,
)
from src.cover_assets import cache_path, normalize_manual_cover, restore_cover, upload_github_cover
from src.metadata_gen import build_cover_information
