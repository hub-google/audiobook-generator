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

# The GUI uses the same src module identity as workers and test fixtures.
# Avoid mutating sys.path or silently suppressing missing dependencies.
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
    format_chapter_label, is_task_active, mark_task_completed, mark_task_interrupted, mark_task_waiting_retry,
    mark_tasks_completed, move_chapter_order, move_tasks, move_tasks_to_pending,
    new_task, normalize_chapter_order, requeue_task_after_active, retry_failed_preflight_stages, settle_interrupted_task,
    task_id_from_run_name, update_task, update_task_chapters,
)
from src.github_run_status import (
    error_observation, missing_observation, observation_text, successful_observation,
)
from src.cover_assets import cache_path, normalize_manual_cover, restore_cover, upload_github_cover
from src.metadata_gen import build_cover_information

from gui_components.source_ui import (
    AUTO_SOURCE_LABEL, SOURCE_EXAMPLES, SOURCE_OPTIONS, SOURCE_NAMES,
    catalog_matches_current, detect_source, normalize_source_url,
    source_hint, source_id_from_label, source_label,
)

from gui_components import QueueOperationsMixin, ReviewMixin, WorkflowMixin
from gui_components.tool_launchers import ToolLauncherMixin
from gui_components.queue_monitor import QueueMonitorMixin
from gui_components.publish_settings import PublishSettingsMixin

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

class AudiobookGUIApp(ReviewMixin, QueueOperationsMixin, WorkflowMixin, QueueMonitorMixin, PublishSettingsMixin, ToolLauncherMixin):
    def __init__(self, root):
        self.root = root
        self.root.title("📚 控制台")
        self.root.geometry("1100x850")
        self.root.minsize(820, 700)

        # 狀態變數
        self.catalog_data = None
        self.cloud_queue = None
        self.queue_syncing = False
        self.queue_sync_after = None
        self.queue_freshness_after = None
        self.task_progress_windows = {}
        self.queue_status_cache = {}
        self.github_observations = {}
        self.editing_task_id = None
        self.catalog_load_token = 0
        self.renumber_selected_chapters = False
        self.chapter_order = []
        self.chapter_title_overrides = {}
        self.chapter_normalized_number_overrides = {}
        self.cleaner_remove_patterns = []
        self.duplicate_detection = {"use_normalized_number": True, "use_chapter_name": True, "use_number_and_name": False}
        self.current_book_profile = {}

        # 發布排程設定變數 (台灣時區 UTC+8)
        self.publish_mode_var = tk.StringVar(value="immediate")
        taipei_now = datetime.now(timezone(timedelta(hours=8)))
        self.publish_date_var = tk.StringVar(value=taipei_now.strftime("%Y-%m-%d"))
        self.publish_time_var = tk.StringVar(value="18:00")
        self.publish_preview_var = tk.StringVar()

        self._setup_style()
        self._build_ui()

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        BG_COLOR = "#f5f6fa"
        self.root.configure(bg=BG_COLOR)
        
        style.configure("TLabel", background=BG_COLOR, font=("Microsoft JhengHei", 10))
        style.configure("Header.TLabel", font=("Microsoft JhengHei", 11, "bold"))
        style.configure("Title.TLabel", font=("Microsoft JhengHei", 14, "bold"), foreground="#192a56")
        style.configure("Status.TLabel", font=("Microsoft JhengHei", 10, "bold"), foreground="#44bd32")
        
        style.configure("TButton", font=("Microsoft JhengHei", 10, "bold"), padding=5)
        style.configure("Accent.TButton", font=("Microsoft JhengHei", 11, "bold"), background="#0097e6", foreground="white")
        style.map("Accent.TButton", background=[("active", "#00a8ff")])

        style.configure("TLabelframe", background=BG_COLOR, padding=10)
        style.configure("TLabelframe.Label", background=BG_COLOR, font=("Microsoft JhengHei", 10, "bold"))

    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        self._build_queue_ui(main_frame)

        notebook = ttk.Notebook(main_frame)
        notebook.pack(fill=tk.BOTH, expand=True)
        settings_tab = ttk.Frame(notebook, padding=(4, 8))
        publish_tab = ttk.Frame(notebook, padding=(4, 8))
        cloud_tab = ttk.Frame(notebook, padding=(4, 8))
        tools_tab = ttk.Frame(notebook, padding=(10, 12))
        notebook.add(cloud_tab, text="雲端執行日誌")
        notebook.add(settings_tab, text="新增小說／章節設定")
        notebook.add(publish_tab, text="公開時間設定")
        notebook.add(tools_tab, text="延伸工具")
        self._build_publish_tab_ui(publish_tab)
        self._build_tools_tab_ui(tools_tab)

        self.selected_status_frame = ttk.LabelFrame(cloud_tab, text="選取小說目前狀態")
        self.selected_status_frame.pack(fill=tk.X, pady=(0, 10))
        self.selected_status_var = tk.StringVar(value="請從上方雲端小說佇列選取一本小說。")
        ttk.Label(self.selected_status_frame, textvariable=self.selected_status_var, justify=tk.LEFT).pack(anchor=tk.W)

        # ── 區塊 1: 目錄網址與章節解析 ──
        section1 = ttk.LabelFrame(settings_tab, text="1. 目錄解析與範圍選取")
        section1.pack(fill=tk.X, pady=(0, 15))

        mode_frame = ttk.Frame(section1)
        mode_frame.pack(fill=tk.X, pady=(0, 5))
        self.edit_mode_var = tk.StringVar(value="新增小說")
        ttk.Label(mode_frame, text="編輯模式：", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(mode_frame, textvariable=self.edit_mode_var, style="Status.TLabel").pack(side=tk.LEFT)
        ttk.Button(mode_frame, text="清除選取／改為新增小說", command=self.reset_chapter_editor).pack(side=tk.RIGHT)

        url_frame = ttk.Frame(section1)
        url_frame.pack(fill=tk.X, pady=5)

        ttk.Label(url_frame, text="來源網站：").pack(side=tk.LEFT, padx=(0, 5))
        self.source_choice_var = tk.StringVar(value=AUTO_SOURCE_LABEL)
        self.source_combo = ttk.Combobox(
            url_frame, textvariable=self.source_choice_var, values=SOURCE_OPTIONS,
            state="readonly", width=14,
        )
        self.source_combo.pack(side=tk.LEFT, padx=(0, 10))
        self.source_combo.bind("<<ComboboxSelected>>", self._on_source_selected)
        ttk.Label(url_frame, text="網址：").pack(side=tk.LEFT, padx=(0, 5))
        self.url_entry = ttk.Entry(url_frame, width=40)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.url_entry.bind("<KeyRelease>", self._on_catalog_url_changed)
        self.url_entry.bind("<FocusOut>", self._on_catalog_url_changed)

        self.btn_parse = ttk.Button(url_frame, text="🔍 解析章節", command=self.start_parse_catalog)
        self.btn_parse.pack(side=tk.LEFT)
        self.btn_preview_chapter = ttk.Button(
            url_frame, text="試抓首章", command=self.preview_first_chapter, state=tk.DISABLED,
        )
        self.btn_preview_chapter.pack(side=tk.LEFT, padx=(6, 0))
        self.source_hint_var = tk.StringVar(value="支援 9 個小說網站；貼上書籍、目錄或章節網址後自動辨識。")
        ttk.Label(section1, textvariable=self.source_hint_var, foreground="#475569").pack(
            anchor=tk.W, padx=4, pady=(0, 3),
        )

        # 解析結果顯示區
        info_frame = ttk.Frame(section1)
        info_frame.pack(fill=tk.X, pady=5)

        self.lbl_book_info = ttk.Label(info_frame, text="書名: 尚未解析 | 總章節: 0 章", style="Header.TLabel")
        self.lbl_book_info.pack(side=tk.LEFT)

        # 章節範圍輸入區
        range_frame = ttk.Frame(section1)
        range_frame.pack(fill=tk.X, pady=5)

        ttk.Label(range_frame, text="開始章節:").pack(side=tk.LEFT, padx=(0, 5))
        self.entry_start = ttk.Entry(range_frame, width=8)
        self.entry_start.insert(0, "1")
        self.entry_start.pack(side=tk.LEFT, padx=(0, 15))

        ttk.Label(range_frame, text="結束章節:").pack(side=tk.LEFT, padx=(0, 5))
        self.entry_end = ttk.Entry(range_frame, width=8)
        self.entry_end.insert(0, "10")
        self.entry_end.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_filter = ttk.Button(range_frame, text="篩選章節", command=self._open_chapter_filter_dialog, state=tk.DISABLED)
        self.btn_filter.pack(side=tk.LEFT, padx=(0, 15))

        self.chapter_selection_var = tk.StringVar(value="尚未解析章節")
        ttk.Label(section1, textvariable=self.chapter_selection_var).pack(anchor=tk.W, pady=(3, 5))

        editor_actions = ttk.Frame(section1)
        editor_actions.pack(fill=tk.X, pady=(5, 0))
        self.btn_add_queue = ttk.Button(editor_actions, text="➕ 新增至雲端小說佇列", style="Accent.TButton", command=self.enqueue_current_task, state=tk.DISABLED)
        self.btn_add_queue.pack(side=tk.RIGHT)
        self.btn_update_queue = ttk.Button(editor_actions, text="💾 更新選取小說章節設定", style="Accent.TButton", command=self.update_selected_task_chapters, state=tk.DISABLED)
        
        self.excluded_chapters = set()


        # ── 區塊 2: 雲端執行日誌 ──
        # 每本小說的控制都集中在上方佇列；舊版的全域控制容易讓人誤以為
        # 它們會操作整個佇列，因此只保留真正有用的雲端日誌。
        section2 = ttk.LabelFrame(cloud_tab, text="2. 雲端執行日誌")
        section2.pack(fill=tk.BOTH, expand=True)

        self.lbl_status = ttk.Label(section2, text="就緒", style="Status.TLabel")
        self.lbl_status.pack(fill=tk.X, padx=6, pady=(4, 2))

        # 實時 Log 控制台
        self.log_text = scrolledtext.ScrolledText(section2, height=12, background="#1e1e1e", foreground="#dcdcdc", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 設定超連結標籤
        self.log_text.tag_config("hyperlink", foreground="#4da6ff", underline=1)
        self.log_text.tag_bind("hyperlink", "<Enter>", lambda e: self.log_text.config(cursor="hand2"))
        self.log_text.tag_bind("hyperlink", "<Leave>", lambda e: self.log_text.config(cursor=""))
        self.link_counter = 0

        self.root.after(500, self.sync_cloud_queue)
        self.root.after(1000, self._refresh_observation_freshness)

    def _build_queue_ui(self, parent):
        section = ttk.LabelFrame(parent, text="雲端佇列（關閉 GUI 後仍由 GitHub 繼續）")
        section.pack(fill=tk.X, pady=(0, 12))
        columns = ("position", "book", "source", "range", "duplicates", "status", "verified", "hf", "youtube", "run")
        headings = {
            "position": "順位", "book": "小說", "source": "來源", "range": "章節", "duplicates": "重複章節", "status": "狀態",
            "verified": "GitHub 查證", "hf": "HF", "youtube": "YouTube", "run": "Run",
        }
        widths = {
            "position": 45, "book": 145, "source": 86, "range": 80, "duplicates": 75, "status": 175,
            "verified": 100, "hf": 65, "youtube": 75, "run": 90,
        }
        lists = ttk.Notebook(section)
        lists.pack(fill=tk.X, padx=5, pady=5)

        def build_tree(tab_text):
            page = ttk.Frame(lists)
            lists.add(page, text=tab_text)
            tree = ttk.Treeview(page, columns=columns, show="headings", height=6, selectmode="extended")
            scrollbar = ttk.Scrollbar(page, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=scrollbar.set)
            for key in columns:
                tree.heading(key, text=headings[key])
                tree.column(key, width=widths[key], anchor=tk.CENTER if key != "book" else tk.W)
            tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            return tree

        self.pending_queue_tree = build_tree("進行中／尚未完成")
        self.success_queue_tree = build_tree("已完成（Success）")
        self.queue_tree = self.pending_queue_tree
        for tree in (self.pending_queue_tree, self.success_queue_tree):
            tree.bind("<<TreeviewSelect>>", lambda event, source=tree: self._activate_queue_tree(source, event))
            tree.bind("<Double-1>", lambda event, source=tree: self._activate_and_open_queue_task(source, event))

        buttons = ttk.Frame(section)
        buttons.pack(fill=tk.X, padx=5, pady=(0, 5))
        ttk.Button(buttons, text="批量加入網址", command=self.open_batch_queue_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="↑", width=3, command=lambda: self.move_selected_task(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="↓", width=3, command=lambda: self.move_selected_task(1)).pack(side=tk.LEFT, padx=2)
        self.btn_toggle_task = ttk.Button(
            buttons, text="暫停/恢復", command=self.toggle_selected_task, state=tk.DISABLED,
        )
        self.btn_toggle_task.pack(side=tk.LEFT, padx=2)
        self.btn_stop_task = ttk.Button(
            buttons, text="取消本次 Run", command=self.stop_selected_task, state=tk.DISABLED,
        )
        self.btn_stop_task.pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="重新排程", command=self.requeue_selected_task).pack(side=tk.LEFT, padx=2)
        self.btn_toggle_completed = ttk.Button(
            buttons, text="標記為已完成", command=self.toggle_completed_selected_task, state=tk.DISABLED,
        )
        self.btn_toggle_completed.pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="刪除", command=self.delete_selected_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="查看進度", command=self.open_selected_task_progress).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="立即同步", command=self.sync_cloud_queue).pack(side=tk.RIGHT, padx=2)
        ttk.Button(buttons, text="執行調度檢查", command=self.trigger_queue_dispatcher).pack(side=tk.RIGHT, padx=2)

        review_buttons = ttk.Frame(section)
        review_buttons.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.btn_batch_cover_info = ttk.Button(
            review_buttons, text="🎨 封面圖／Gemini 小說介紹／HF 生圖 Prompt",
            command=self.open_cover_preflight_review, state=tk.DISABLED,
        )
        self.btn_batch_cover_info.pack(side=tk.LEFT, padx=2)
        self.btn_sample_text = ttk.Button(
            review_buttons, text="🔍 文字清理／關鍵字審核",
            command=self.open_ad_analysis_results, state=tk.DISABLED,
        )
        self.btn_sample_text.pack(side=tk.LEFT, padx=2)
        self.btn_start_processing = ttk.Button(
            review_buttons, text="開始第二階段後製", command=self.start_selected_processing,
            state=tk.DISABLED,
        )
        self.btn_start_processing.pack(side=tk.LEFT, padx=2)
        ttk.Label(
            review_buttons, text="語音品質檢查：Clean 欄就是 TTS 實際會朗讀的文字",
        ).pack(side=tk.LEFT, padx=8)

    def _activate_queue_tree(self, tree, event=None):
        self.queue_tree = tree
        other = self.success_queue_tree if tree is self.pending_queue_tree else self.pending_queue_tree
        if tree.selection():
            other.selection_remove(*other.selection())
        self._on_queue_select(event)

    def _activate_and_open_queue_task(self, tree, event=None):
        self._activate_queue_tree(tree, event)
        self.open_selected_task_progress(event)

    def _github_settings(self):
        load_dotenv(ENV_PATH, override=True)
        repo = os.getenv("GITHUB_REPO", "hub-google/audiobook-generator")
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            gh_candidates = [
                r"C:\Program Files\GitHub CLI\gh.exe",
                shutil.which("gh"),
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "GitHub CLI", "gh.exe"),
            ]
            for gh_path in gh_candidates:
                if gh_path and os.path.exists(gh_path):
                    try:
                        res = subprocess.run([gh_path, "auth", "token"], capture_output=True, text=True, timeout=5)
                        if res.returncode == 0 and res.stdout.strip():
                            token = res.stdout.strip()
                            break
                    except Exception:
                        pass
        if not token:
            raise RuntimeError("本地 .env 中未找到 GITHUB_TOKEN，且未檢測到 GitHub CLI 登入")
        return repo, token

    def _queue_store(self):
        if GitHubQueueStore is None:
            raise RuntimeError("雲端佇列模組載入失敗；請檢查 src/cloud_queue.py 與 requests 套件")
        repo, token = self._github_settings()
        return GitHubQueueStore(repo, token), repo, token

    def _profile_store(self):
        repo, token = self._github_settings()
        return GitHubBookProfileStore(repo, token), repo, token

    def _render_queue(self, queue):
        selected_ids = {
            tree: tuple(tree.selection())
            for tree in (self.pending_queue_tree, self.success_queue_tree)
        }
        for tree in (self.pending_queue_tree, self.success_queue_tree):
            tree.delete(*tree.get_children())
        for task in queue.get("queue", []) + queue.get("completed", []):
            start = task.get("start_chapter") or 1
            end = task.get("end_chapter") or "全部"
            hf = task.get("hf_progress") or {}
            yt = task.get("youtube_progress") or {}
            observation = self.github_observations.get(task.get("task_id"))
            duplicate_count = task.get("duplicate_chapter_count")
            target_tree = self.success_queue_tree if task in queue.get("completed", []) else self.pending_queue_tree
            adapter = detect_source(task.get("catalog_url") or "")
            source_name = source_label(adapter.source_id) if adapter else "未知來源"
            target_tree.insert("", tk.END, iid=task["task_id"], values=(
                task.get("position", "—"), task.get("book_title"), source_name, f"{start}–{end}",
                duplicate_count if duplicate_count is not None else "—", self._queue_status_text(task),
                self._observation_checked_time(observation),
                f"{hf.get('completed', 0)}/{hf.get('total', 0)}",
                f"{yt.get('completed', 0)}/{yt.get('total', 0)}",
                self._task_run_summary(task),
            ))
            previous = self.queue_status_cache.get(task["task_id"])
            current = (task.get("status"), task.get("run_id"))
            if previous != current:
                status, run_id = current
                title = task.get("book_title") or "待解析"
                if status == "running" and run_id:
                    self.log(f"🚀 {title} 已開始執行｜Run {run_id}")
                elif status == "completed":
                    self.log(f"✅ {title} 製作完成")
                elif status == "waiting_review":
                    self.log(f"✅ {title} 第一階段完成：TXT 與封面都已完成，等待人工審核。")
                elif status == "waiting_retry":
                    self.log(f"⏳ {title} 暫停等待安全重試")
                elif status == "needs_attention":
                    self.log(f"⚠ {title} 需要處理：{task.get('reason') or '未知原因'}")
                elif status == "interrupted":
                    self.log(f"🛑 {title} 本次 Run 已中斷；任務已保留，可按「重新排程」")
            self.queue_status_cache[task["task_id"]] = current
        restored_ids = []
        for tree, ids in selected_ids.items():
            valid = [task_id for task_id in ids if tree.exists(task_id)]
            if valid:
                tree.selection_set(valid)
                self.queue_tree = tree
                restored_ids.extend(valid)
        if restored_ids:
            selected_tasks = [
                item for item in queue.get("queue", []) + queue.get("completed", []) if item.get("task_id") in restored_ids
            ]
            self._update_queue_control_states(selected_tasks)
            if len(selected_tasks) == 1:
                self._update_selected_task_status(selected_tasks[0])
            else:
                self.selected_status_var.set(f"已選取 {len(selected_tasks)} 筆小說任務；可批次暫停、刪除或調整順位。")
        else:
            self._update_queue_control_states(None)

    def _queue_status_text(self, task):
        stages = task.get("stages") or {}
        scrape = (stages.get("scrape") or {}).get("status", "pending")
        cover = (stages.get("cover") or {}).get("status", "pending")
        if task.get("workflow_phase") == "preflight":
            if task.get("status") == "waiting_review":
                reviewed = (task.get("ad_review") or {}).get("status") == "approved"
                return "② 清理設定已確認｜等待開始後製" if reviewed else "② 待人工審核｜TXT✓ 封面✓"
            if task.get("status") == "needs_attention":
                failed = []
                if scrape == "failed": failed.append("TXT失敗")
                if cover == "failed": failed.append("封面失敗")
                return "① " + ("／".join(failed) or "前置作業需要處理")
            labels = {"pending": "待啟動", "dispatching": "啟動中", "running": "處理中", "completed": "✓", "failed": "失敗"}
            return f"① TXT {labels.get(scrape, scrape)}｜封面 {labels.get(cover, cover)}"
        if task.get("status") == "processing":
            return "③ 後製處理中"
        if task.get("status") == "completed":
            return "④ 已完成"
        # A bound Run's verified GitHub state is authoritative. Queue-control
        # state must never hide an in-progress (or otherwise verified) Run.
        if task.get("run_id"):
            observation = self.github_observations.get(task.get("task_id"))
            if observation:
                return observation_text(observation)
            return task.get("status") or "idle"
        status = task.get("status") or "idle"
        if status == "queued":
            return "① 等待啟動"
        return status

    @staticmethod
    def _task_run_summary(task):
        if task.get("workflow_phase") == "preflight":
            return f"TXT {task.get('scrape_run_id') or '—'} / 封面 {task.get('cover_run_id') or '—'}"
        return str(task.get("processing_run_id") or task.get("run_id") or "—")

    def _update_selected_task_status(self, task):
        hf = task.get("hf_progress") or {}
        yt = task.get("youtube_progress") or {}
        run_text = task.get("run_id") or "尚未建立"
        extra = ""
        if task.get("requeue_after_edit"):
            extra = "\n章節設定已更新；正在停止舊 Run，確認停止後會自動重新排程。"
        stages = task.get("stages") or {}
        stage_text = ""
        if task.get("workflow_phase") == "preflight":
            scrape = stages.get("scrape") or {}; cover = stages.get("cover") or {}
            stage_text = (
                f"\n第一階段：TXT {scrape.get('status', 'pending')} (Run {task.get('scrape_run_id') or '—'})"
                f"｜封面/Gemini {cover.get('status', 'pending')} (Run {task.get('cover_run_id') or '—'})"
                f"\n人工審核：{(task.get('ad_review') or {}).get('status', 'pending')}"
            )
        elif task.get("workflow_phase") == "processing":
            stage_text = f"\n第三階段：後製／上傳 (Run {task.get('processing_run_id') or task.get('run_id') or '—'})"
        publish_note = ""
        if task.get("publish_at"):
            publish_note = f"\n排定公開（台灣時間）：{task.get('publish_at')}"
        self.selected_status_var.set(
            f"《{task.get('book_title') or '待解析'}》｜"
            f"來源：{source_label(detect_source(task.get('catalog_url') or '').source_id) if detect_source(task.get('catalog_url') or '') else '未知來源'}｜"
            f"第 {task.get('start_chapter') or 1}～{task.get('end_chapter') or '最後'} 章\n"
            f"狀態：{self._queue_status_text(task)}　｜　GitHub Run：{run_text}\n"
            f"HF：{hf.get('completed', 0)}/{hf.get('total', 0)}　｜　YouTube：{yt.get('completed', 0)}/{yt.get('total', 0)}{publish_note}{stage_text}{extra}"
        )

    def _on_source_selected(self, _event=None):
        source_id = source_id_from_label(self.source_choice_var.get())
        if source_id is None:
            self._on_catalog_url_changed()
            return
        raw = self.url_entry.get().strip()
        current_source = detect_source(raw)
        # Explicitly selecting a source fills an example only for an empty
        # field or a previous example; never overwrite a custom book URL.
        if not raw or raw in SOURCE_EXAMPLES.values():
            self.url_entry.delete(0, tk.END)
            self.url_entry.insert(0, SOURCE_EXAMPLES[source_id])
            self._on_catalog_url_changed()
        elif current_source and current_source.source_id != source_id:
            self.source_hint_var.set("已保留原輸入網址；請貼上所選網站的網址，再解析目錄。")
        else:
            self._on_catalog_url_changed()

    def _on_catalog_url_changed(self, _event=None):
        raw = self.url_entry.get().strip()
        source = detect_source(raw)
        if source is None:
            self.source_hint_var.set(
                "支援 9 個小說網站；請輸入完整的 http(s) 書籍、目錄或章節網址。"
            )
        else:
            self.source_choice_var.set(source_label(source.source_id))
            self.source_hint_var.set(source_hint(source))
        current = catalog_matches_current(raw, self.catalog_data)
        if hasattr(self, "btn_add_queue"):
            active = not self.editing_task_id and current
            self.btn_add_queue.config(state=tk.NORMAL if active else tk.DISABLED)
        if hasattr(self, "btn_preview_chapter"):
            self.btn_preview_chapter.config(state=tk.NORMAL if current else tk.DISABLED)
        if self.editing_task_id and hasattr(self, "btn_update_queue") and self.catalog_data:
            self.btn_update_queue.config(
                state=tk.NORMAL if catalog_matches_current(raw, self.catalog_data) else tk.DISABLED
            )

    def reset_chapter_editor(self):
        self.catalog_load_token += 1
        self.editing_task_id = None
        self.catalog_data = None
        self.current_book_profile = {}
        self.chapter_title_overrides = {}
        self.chapter_normalized_number_overrides = {}
        self.cleaner_remove_patterns = []
        self.duplicate_detection = {"use_normalized_number": True, "use_chapter_name": True, "use_number_and_name": False}
        self.excluded_chapters.clear()
        self.renumber_selected_chapters = False
        self.chapter_order = []
        for tree in (self.pending_queue_tree, self.success_queue_tree):
            tree.selection_remove(*tree.selection())
        self.edit_mode_var.set("新增小說")
        self.url_entry.delete(0, tk.END)
        self.source_choice_var.set(AUTO_SOURCE_LABEL)
        self._on_catalog_url_changed()
        self.lbl_book_info.config(text="書名: 尚未解析 | 總章節: 0 章")
        self.entry_start.delete(0, tk.END)
        self.entry_start.insert(0, "1")
        self.entry_end.delete(0, tk.END)
        self.entry_end.insert(0, "10")
        self.chapter_selection_var.set("尚未解析章節")
        self.btn_filter.config(state=tk.DISABLED)
        self.btn_preview_chapter.config(state=tk.DISABLED)
        self.btn_update_queue.pack_forget()
        self.btn_add_queue.pack(side=tk.RIGHT)
        self._set_configured_publish_at("")

    def _load_queue_task_for_edit(self, task):
        self.catalog_load_token += 1
        token = self.catalog_load_token
        self.editing_task_id = task["task_id"]
        self.edit_mode_var.set(f"正在編輯：《{task.get('book_title') or '待解析'}》")
        self.btn_add_queue.pack_forget()
        self.btn_update_queue.pack(side=tk.RIGHT)
        self.btn_update_queue.config(state=tk.DISABLED)
        self.url_entry.delete(0, tk.END)
        self.url_entry.insert(0, task.get("catalog_url") or "")
        self._on_catalog_url_changed()
        self.lbl_book_info.config(text=f"書名: {task.get('book_title') or '待解析'} | 正在載入章節…")
        self._set_configured_publish_at(task.get("publish_at") or "")

        def worker():
            try:
                result = parse_catalog(task.get("catalog_url") or "")
                try:
                    profiles, _ = self._profile_store()[0].load()
                    _, profile = get_book_profile(
                        profiles, task.get("catalog_url") or "", task.get("book_title") or "",
                    )
                except Exception as profile_error:
                    profile = {}
                    detail = str(profile_error)
                    self.root.after(
                        0,
                        lambda message=detail: self.log(
                            f"⚠ 雲端書籍設定暫時無法讀取，將使用佇列中的章節設定：{message}"
                        ),
                    )
                overrides = profile.get("chapter_title_overrides") or task.get("chapter_title_overrides") or {}
                normalized_overrides = (
                    profile.get("chapter_normalized_number_overrides") or
                    task.get("chapter_normalized_number_overrides") or {}
                )
                apply_chapter_title_overrides(result, overrides, normalized_overrides)
                self.root.after(0, lambda: self._finish_queue_task_load(token, task["task_id"], result, profile))
            except Exception as error:
                self.root.after(0, lambda detail=str(error): self._on_parse_failed(detail))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_queue_task_load(self, token, task_id, result, profile=None):
        if token != self.catalog_load_token or task_id != self.editing_task_id:
            return
        task = self._selected_task()
        if not task or task.get("task_id") != task_id:
            return
        self._on_parse_success(result)
        if not result or not result.get("success"):
            return
        total = int(result.get("total_chapters") or 0)
        start = max(1, min(int(task.get("start_chapter") or 1), total))
        end = max(start, min(int(task.get("end_chapter") or total), total))
        self.entry_start.delete(0, tk.END)
        self.entry_start.insert(0, str(start))
        self.entry_end.delete(0, tk.END)
        self.entry_end.insert(0, str(end))
        self.excluded_chapters = {int(value) for value in task.get("excluded_chapters") or []}
        self.renumber_selected_chapters = bool(task.get("renumber_selected"))
        self.chapter_order = normalize_chapter_order(
            task.get("chapter_order"), task.get("start_chapter") or 1, task.get("end_chapter"),
        )
        profile = profile or {}
        self.chapter_title_overrides = dict(profile.get("chapter_title_overrides") or task.get("chapter_title_overrides") or {})
        self.chapter_normalized_number_overrides = dict(
            profile.get("chapter_normalized_number_overrides") or
            task.get("chapter_normalized_number_overrides") or {}
        )
        self.cleaner_remove_patterns = list(profile.get("cleaner_remove_patterns") or [])
        self.duplicate_detection = dict(profile.get("duplicate_detection") or self.duplicate_detection)
        self._update_chapter_selection_summary(start, end)
        self.btn_update_queue.config(state=tk.NORMAL)


    def _update_chapter_selection_summary(self, start=None, end=None):
        try:
            start = int(start if start is not None else self.entry_start.get())
            end = int(end if end is not None else self.entry_end.get())
        except ValueError:
            return
        excluded = sorted(value for value in self.excluded_chapters if start <= value <= end)
        selected = max(0, end - start + 1 - len(excluded))
        excluded_text = "無" if not excluded else "、".join(map(str, excluded[:12])) + ("…" if len(excluded) > 12 else "")
        numbering = "　｜　輸出章號：依已選順序重新編號" if self.renumber_selected_chapters else ""
        self.chapter_selection_var.set(f"已選擇：{selected} 章　｜　已排除：{excluded_text}{numbering}")


if __name__ == "__main__":
    try:
        from tkinterdnd2 import TkinterDnD
        root = TkinterDnD.Tk()
    except ImportError:
        root = tk.Tk()
    app = AudiobookGUIApp(root)
    root.mainloop()
