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
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
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

from gui_components import QueueOperationsMixin, ReviewMixin, WorkflowMixin

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

class AudiobookGUIApp(ReviewMixin, QueueOperationsMixin, WorkflowMixin):
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
        cloud_tab = ttk.Frame(notebook, padding=(4, 8))
        notebook.add(cloud_tab, text="雲端執行日誌")
        notebook.add(settings_tab, text="新增小說／章節設定")

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

        ttk.Label(url_frame, text="目錄網址:").pack(side=tk.LEFT, padx=(0, 5))
        self.url_entry = ttk.Entry(url_frame, width=50)
        self.url_entry.insert(0, "https://tw.hjwzw.com/Book/Chapter/1644")
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.btn_parse = ttk.Button(url_frame, text="🔍 解析章節", command=self.start_parse_catalog)
        self.btn_parse.pack(side=tk.LEFT)

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
        self.btn_add_queue = ttk.Button(editor_actions, text="➕ 新增至雲端小說佇列", style="Accent.TButton", command=self.enqueue_current_task)
        self.btn_add_queue.pack(side=tk.RIGHT)
        self.btn_run = self.btn_add_queue
        self.btn_update_queue = ttk.Button(editor_actions, text="💾 更新選取小說章節設定", style="Accent.TButton", command=self.update_selected_task_chapters, state=tk.DISABLED)
        
        self.excluded_chapters = set()


        # ── 區塊 2: 雲端執行日誌 ──
        # 每本小說的控制都集中在上方佇列；舊版的全域控制容易讓人誤以為
        # 它們會操作整個佇列，因此只保留真正有用的雲端日誌。
        section2 = ttk.LabelFrame(cloud_tab, text="2. 雲端執行日誌")
        section2.pack(fill=tk.BOTH, expand=True)

        # 保留元件供舊版單一 Run 相容程式使用，但不放進新介面。
        action_frame = ttk.Frame(section2)
        self.btn_cancel = ttk.Button(action_frame, text="🛑 取消雲端作業", command=self.cancel_github_actions, state=tk.DISABLED)
        self.btn_download = ttk.Button(action_frame, text="📥 一鍵下載成品", command=self.start_batch_download, state=tk.DISABLED)
        self.lbl_status = ttk.Label(action_frame, text="就緒", style="Status.TLabel")
        self.progress_bar = ttk.Progressbar(section2, mode="indeterminate")

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
        columns = ("position", "book", "range", "duplicates", "status", "verified", "hf", "youtube", "run")
        headings = {
            "position": "順位", "book": "小說", "range": "章節", "duplicates": "重複章節", "status": "狀態",
            "verified": "GitHub 查證", "hf": "HF", "youtube": "YouTube", "run": "Run",
        }
        widths = {
            "position": 45, "book": 135, "range": 80, "duplicates": 75, "status": 175,
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
            target_tree.insert("", tk.END, iid=task["task_id"], values=(
                task.get("position", "—"), task.get("book_title"), f"{start}–{end}",
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

    @staticmethod
    def _observation_checked_time(observation):
        if not observation or not observation.get("checked_at"):
            return "—"
        try:
            value = datetime.fromisoformat(str(observation["checked_at"]).replace("Z", "+00:00"))
            return value.astimezone().strftime("%H:%M:%S")
        except (TypeError, ValueError):
            return "無效時間"

    @staticmethod
    def _http_error_observation(response):
        status = response.status_code
        if status == 401:
            code = "unauthorized"
        elif status == 429 or response.headers.get("X-RateLimit-Remaining") == "0":
            code = "rate_limited"
        elif status == 403:
            code = "forbidden"
        elif status >= 500:
            code = "github_error"
        else:
            code = "invalid_response"
        return error_observation(code, http_status=status, detail=response.text[:500])

    def _observe_github_run(self, repo, token, run_id, previous):
        headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for attempt in range(2):
            try:
                response = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/runs/{run_id}",
                    headers=headers, timeout=15,
                )
                if response.status_code == 200:
                    try:
                        return successful_observation(response.json())
                    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
                        return error_observation("invalid_response", http_status=200, detail=error)
                if response.status_code == 404:
                    verification = requests.get(
                        f"https://api.github.com/repos/{repo}/actions/runs?per_page=1",
                        headers=headers, timeout=15,
                    )
                    if verification.status_code != 200:
                        return self._http_error_observation(verification)
                    return missing_observation(previous, confirmed=True)
                return self._http_error_observation(response)
            except requests.RequestException as error:
                if attempt == 1:
                    return error_observation("network_error", detail=error)
                time.sleep(1)

    def _refresh_observation_freshness(self):
        """Expire an old observation in the UI even while a network poll is blocked."""
        if self.cloud_queue:
            for task in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", []):
                task_id = task.get("task_id")
                display_tree = next((tree for tree in (self.pending_queue_tree, self.success_queue_tree) if tree.exists(task_id)), None)
                if task_id and task.get("run_id") and display_tree is not None:
                    observation = self.github_observations.get(task_id)
                    display_tree.set(task_id, "status", self._queue_status_text(task))
                    display_tree.set(task_id, "verified", self._observation_checked_time(observation))
        self.queue_freshness_after = self.root.after(1000, self._refresh_observation_freshness)

    def sync_cloud_queue(self):
        if self.queue_sync_after is not None:
            try:
                self.root.after_cancel(self.queue_sync_after)
            except Exception:
                pass
            self.queue_sync_after = None
        if self.queue_syncing:
            return
        self.queue_syncing = True
        def worker():
            try:
                store, repo, token = self._queue_store()
                queue, _ = store.load()
                terminal_updates = []
                running_updates = []
                preflight_updates = {}
                # Preflight owns two independent Runs.  The queue dispatcher
                # reconciles both atomically; the legacy single-Run observer
                # must not mark the book complete when only TXT has finished.
                monitored = [
                    task for task in queue.get("queue", [])
                    if task.get("run_id") and task.get("workflow_phase") != "preflight"
                ]
                observations = {}
                preflight_stages = []
                for task in queue.get("queue", []):
                    if task.get("workflow_phase") != "preflight":
                        continue
                    for stage_name, prefix, workflow_file in (
                        ("scrape", f"scrape-review-{task.get('task_id')}", "audiobook.yml"),
                        ("cover", f"cover-review-{task.get('task_id')}", "cover-preflight.yml"),
                    ):
                        run_id = task.get(f"{stage_name}_run_id") or \
                            ((task.get("stages") or {}).get(stage_name) or {}).get("run_id")
                        if not run_id:
                            stage = ((task.get("stages") or {}).get(stage_name) or {})
                            discovered = self._discover_preflight_run(
                                repo, token, workflow_file, task["task_id"],
                                stage.get("dispatched_at") or task.get("dispatched_at"),
                            )
                            run_id = discovered.get("id") if discovered else None
                        if run_id:
                            preflight_stages.append((task["task_id"], stage_name, int(run_id), prefix))
                worker_count = min(8, max(1, len(monitored) + len(preflight_stages)))
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = {
                        executor.submit(
                            self._observe_github_run, repo, token, task["run_id"],
                            self.github_observations.get(task["task_id"]),
                        ): task
                        for task in monitored
                    }
                    preflight_futures = {
                        executor.submit(
                            self._observe_preflight_stage, repo, token, run_id, prefix,
                        ): (task_id, stage_name, run_id)
                        for task_id, stage_name, run_id, prefix in preflight_stages
                    }
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            observations[task["task_id"]] = future.result()
                        except Exception as error:
                            observations[task["task_id"]] = error_observation("network_error", detail=error)
                    for future in as_completed(preflight_futures):
                        task_id, stage_name, run_id = preflight_futures[future]
                        try:
                            result = future.result()
                        except Exception as error:
                            logging.warning("Could not reconcile %s %s Run %s: %s", task_id, stage_name, run_id, error)
                            continue
                        preflight_updates.setdefault(task_id, {})[stage_name] = {
                            "run_id": run_id, **result,
                        }
                for task in monitored:
                    run_id = task["run_id"]
                    observation = observations[task["task_id"]]
                    self.github_observations[task["task_id"]] = observation
                    if observation.get("kind") == "ok" and observation.get("raw_status") != "completed":
                        if task.get("status") != "running":
                            running_updates.append((task["task_id"], run_id))
                    elif task.get("status") in BLOCKING_STATES:
                        if observation.get("kind") == "not_found" and observation.get("confirmed_missing"):
                            terminal_updates.append((task["task_id"], run_id, "interrupted", "run_not_found", "missing", None))
                        elif (observation.get("kind") == "ok" and
                              observation.get("raw_status") == "completed" and
                              observation.get("raw_conclusion") == "cancelled"):
                            terminal_updates.append((
                                task["task_id"], run_id, "interrupted", "run_cancelled", "cancelled",
                                observation.get("github_updated_at"),
                            ))
                        elif (observation.get("kind") == "ok" and
                              observation.get("raw_status") == "completed" and
                              observation.get("raw_conclusion") == "success"):
                            terminal_updates.append((
                                task["task_id"], run_id, "completed", None, "success",
                                observation.get("github_updated_at"),
                            ))
                        elif (observation.get("kind") == "ok" and
                              observation.get("raw_status") == "completed" and
                              observation.get("raw_conclusion") != "success"):
                            conclusion = observation.get("raw_conclusion") or "unknown"
                            terminal_updates.append((
                                task["task_id"], run_id, "waiting_retry",
                                f"run_{conclusion}", conclusion,
                                observation.get("github_updated_at"),
                            ))
                if running_updates:
                    def apply_running_updates(latest):
                        for task_id, run_id in running_updates:
                            latest = update_task(latest, task_id, status="running", run_id=run_id, reason=None, run_conclusion=None)
                        return latest
                    queue = store.mutate(apply_running_updates, "Reconcile active running audiobook tasks")
                if terminal_updates:
                    def apply_terminal_updates(latest):
                        for task_id, run_id, target, reason, conclusion, ended_at in terminal_updates:
                            current = next(
                                (item for item in latest.get("queue", []) if item.get("task_id") == task_id), None
                            )
                            if current and current.get("run_id") == run_id and current.get("status") in BLOCKING_STATES:
                                if target == "completed":
                                    latest = mark_task_completed(
                                        latest, task_id, conclusion=conclusion or "success", ended_at=ended_at,
                                    )
                                elif target == "interrupted":
                                    latest = settle_interrupted_task(
                                        latest, task_id, reason=reason,
                                        conclusion=conclusion, ended_at=ended_at,
                                    )
                                else:
                                    if current.get("status") == "waiting_retry" and current.get("retry_at"):
                                        continue
                                    latest = mark_task_waiting_retry(latest, task_id, reason=reason, conclusion=conclusion, ended_at=ended_at)
                        return latest
                    queue = store.mutate(apply_terminal_updates, "Reconcile completed audiobook runs")
                    self._dispatch_queue_workflow()
                if preflight_updates:
                    changed = self._apply_preflight_observations(queue, preflight_updates)
                    if changed:
                        queue = store.mutate(
                            lambda latest: (
                                self._apply_preflight_observations(latest, preflight_updates), latest
                            )[1],
                            "Reconcile completed preflight Runs from GUI",
                        )
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
            except Exception as error:
                if self.cloud_queue:
                    for task in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", []):
                        if task.get("run_id"):
                            self.github_observations[task["task_id"]] = error_observation(
                                "network_error", detail=error,
                            )
                    self.root.after(0, lambda q=self.cloud_queue: self._render_queue(q))
                self.root.after(0, lambda e=str(error): self.log(f"⚠ 雲端佇列同步失敗：{e}"))
            finally:
                self.queue_syncing = False
                self.queue_sync_after = self.root.after(10000, self.sync_cloud_queue)
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _apply_preflight_observations(queue, observations):
        """Apply verified preflight Run states without overwriting a newer Run."""
        changed = False
        for task in queue.get("queue", []):
            task_id = task.get("task_id")
            if task.get("workflow_phase") != "preflight" or task_id not in observations:
                continue
            stages = task.setdefault("stages", {})
            for stage_name, result in observations[task_id].items():
                stage = stages.setdefault(stage_name, {})
                run_id = int(result["run_id"])
                bound_run_id = task.get(f"{stage_name}_run_id") or stage.get("run_id")
                if bound_run_id and int(bound_run_id) != run_id:
                    continue
                next_status = result.get("status")
                if not next_status or stage.get("status") == next_status:
                    continue
                stage.update({"run_id": run_id, "status": next_status})
                task[f"{stage_name}_run_id"] = run_id
                if next_status == "completed":
                    stage.update({"reason": None, "completed_at": result.get("updated_at")})
                elif next_status == "failed":
                    stage["reason"] = result.get("conclusion") or "failure"
                changed = True
            if task.get("status") == "canceling":
                continue
            scrape = (stages.get("scrape") or {}).get("status")
            cover = (stages.get("cover") or {}).get("status")
            next_task_status = (
                "needs_attention" if "failed" in {scrape, cover}
                else "waiting_review" if {scrape, cover} == {"completed"}
                else "preparing_assets"
            )
            if task.get("status") != next_task_status:
                task["status"] = next_task_status
                task["reason"] = None if next_task_status != "needs_attention" else "preflight_failed"
                changed = True
        return changed

    @staticmethod
    def _observe_preflight_stage(repo, token, run_id, artifact_prefix):
        headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(
            f"https://api.github.com/repos/{repo}/actions/runs/{int(run_id)}",
            headers=headers, timeout=15,
        )
        response.raise_for_status()
        run = response.json()
        if run.get("status") != "completed":
            return {"status": "running", "conclusion": None, "updated_at": run.get("updated_at")}
        conclusion = run.get("conclusion")
        if conclusion != "success":
            return {"status": "failed", "conclusion": conclusion, "updated_at": run.get("updated_at")}
        artifacts = requests.get(
            f"https://api.github.com/repos/{repo}/actions/runs/{int(run_id)}/artifacts",
            headers=headers, params={"per_page": 100}, timeout=15,
        )
        artifacts.raise_for_status()
        artifact_ready = any(
            not item.get("expired") and str(item.get("name") or "").startswith(artifact_prefix)
            for item in artifacts.json().get("artifacts", [])
        )
        return {
            "status": "completed" if artifact_ready else "running",
            "conclusion": conclusion,
            "updated_at": run.get("updated_at"),
        }

    @staticmethod
    def _discover_preflight_run(repo, token, workflow_file, task_id, dispatched_at=None):
        """Recover a Run ID lost during workflow_dispatch indexing or a later re-run."""
        headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs",
            headers=headers, params={"event": "workflow_dispatch", "per_page": 100}, timeout=15,
        )
        response.raise_for_status()
        since = None
        if dispatched_at:
            try:
                since = datetime.fromisoformat(str(dispatched_at).replace("Z", "+00:00"))
            except ValueError:
                pass
        matches = []
        for run in response.json().get("workflow_runs", []):
            name = run.get("display_title") or run.get("name")
            if task_id_from_run_name(name) != task_id:
                continue
            if since:
                try:
                    created = datetime.fromisoformat(str(run.get("created_at") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if created < since:
                    continue
            matches.append(run)
        return max(matches, key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)), default=None)

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
        self.selected_status_var.set(
            f"《{task.get('book_title') or '待解析'}》｜第 {task.get('start_chapter') or 1}～{task.get('end_chapter') or '最後'} 章\n"
            f"狀態：{self._queue_status_text(task)}　｜　GitHub Run：{run_text}\n"
            f"HF：{hf.get('completed', 0)}/{hf.get('total', 0)}　｜　YouTube：{yt.get('completed', 0)}/{yt.get('total', 0)}{stage_text}{extra}"
        )

    def reset_chapter_editor(self):
        self.catalog_load_token += 1
        self.editing_task_id = None
        self.catalog_data = None
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
        self.lbl_book_info.config(text="書名: 尚未解析 | 總章節: 0 章")
        self.entry_start.delete(0, tk.END)
        self.entry_start.insert(0, "1")
        self.entry_end.delete(0, tk.END)
        self.entry_end.insert(0, "10")
        self.chapter_selection_var.set("尚未解析章節")
        self.btn_filter.config(state=tk.DISABLED)
        self.btn_update_queue.pack_forget()
        self.btn_add_queue.pack(side=tk.RIGHT)

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
        self.lbl_book_info.config(text=f"書名: {task.get('book_title') or '待解析'} | 正在載入章節…")

        def worker():
            try:
                result = parse_catalog(task.get("catalog_url") or "")
                profiles, _ = self._profile_store()[0].load()
                _, profile = get_book_profile(profiles, task.get("catalog_url") or "", task.get("book_title") or "")
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
