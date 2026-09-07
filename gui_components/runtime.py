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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageTk

# 載入目錄解析器
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(PROJECT_ROOT, "src"))
try:
    from catalog_parser import (
        analyze_duplicate_chapters, apply_chapter_title_overrides, fetch_69shuba_full_novels,
        find_direct_duplicate_matches, parse_catalog, split_chapter_title,
    )
    from chapter_numbers import normalize_positive_chapter_number
    from cleaner import chunk_text, clean_text_content
    from book_profiles import (
        GitHubBookProfileStore, book_profile_id, get_book_profile, profile_snapshot,
        update_book_profile, validate_remove_patterns,
    )
    from crawler import fetch_chapter_text
    from cloud_queue import (
        BLOCKING_STATES, GitHubQueueStore, add_tasks, delete_task, approve_preflight,
        confirm_preflight_review, start_reviewed_processing,
        format_chapter_label, is_task_active, mark_task_completed, mark_task_interrupted, mark_task_waiting_retry,
        mark_tasks_completed, move_chapter_order, move_tasks, move_tasks_to_pending,
        new_task, normalize_chapter_order, requeue_task_after_active, settle_interrupted_task,
        task_id_from_run_name, update_task, update_task_chapters,
    )
    from github_run_status import (
        error_observation, missing_observation, observation_text,
        successful_observation,
    )
    from cover_assets import cache_path, normalize_manual_cover, restore_cover, upload_github_cover
    from metadata_gen import build_cover_information
except ImportError:
    parse_catalog = None
    fetch_69shuba_full_novels = None
    GitHubQueueStore = None

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)

