import os
import sys
import json
import time
import shutil
import subprocess
import requests
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog
from dotenv import load_dotenv
import re
import webbrowser
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 載入目錄解析器
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
try:
    from catalog_parser import analyze_duplicate_chapters, parse_catalog, split_chapter_title
    from cleaner import chunk_text, clean_text_content
    from crawler import fetch_chapter_text
    from cloud_queue import (
        BLOCKING_STATES, GitHubQueueStore, TERMINAL_STATES, add_tasks, delete_task,
        is_task_active, mark_task_interrupted, mark_task_needs_attention, move_task,
        move_tasks, new_task, requeue_task_after_active, update_task, update_task_chapters,
    )
    from github_run_status import (
        error_observation, missing_observation, observation_text,
        successful_observation,
    )
except ImportError:
    parse_catalog = None
    GitHubQueueStore = None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

class AudiobookGUIApp:
    def __init__(self, root):
        self.root = root
        self.root.title("📚 有聲小說雲端控制台")
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
        section = ttk.LabelFrame(parent, text="雲端小說佇列（關閉 GUI 後仍由 GitHub 繼續）")
        section.pack(fill=tk.X, pady=(0, 12))
        columns = ("position", "book", "range", "duplicates", "status", "verified", "hf", "youtube", "run")
        tree_frame = ttk.Frame(section)
        tree_frame.pack(fill=tk.X, padx=5, pady=5)
        self.queue_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=6, selectmode="extended")
        queue_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=queue_scrollbar.set)
        headings = {
            "position": "順位", "book": "小說", "range": "章節", "duplicates": "重複章節", "status": "狀態",
            "verified": "GitHub 查證", "hf": "HF", "youtube": "YouTube", "run": "Run",
        }
        widths = {
            "position": 45, "book": 135, "range": 80, "duplicates": 75, "status": 175,
            "verified": 100, "hf": 65, "youtube": 75, "run": 90,
        }
        for key in columns:
            self.queue_tree.heading(key, text=headings[key])
            self.queue_tree.column(key, width=widths[key], anchor=tk.CENTER if key != "book" else tk.W)
        self.queue_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        queue_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_select)
        self.queue_tree.bind("<Double-1>", self.open_selected_task_progress)

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
        ttk.Button(buttons, text="刪除", command=self.delete_selected_task).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="查看進度", command=self.open_selected_task_progress).pack(side=tk.LEFT, padx=2)
        ttk.Button(buttons, text="立即同步", command=self.sync_cloud_queue).pack(side=tk.RIGHT, padx=2)
        ttk.Button(buttons, text="執行調度檢查", command=self.trigger_queue_dispatcher).pack(side=tk.RIGHT, padx=2)

        review_buttons = ttk.Frame(section)
        review_buttons.pack(fill=tk.X, padx=5, pady=(0, 5))
        self.btn_sample_text = ttk.Button(
            review_buttons, text="🔍 抽查第一／中間／最後章 Raw 與 Clean",
            command=self.open_text_sample, state=tk.DISABLED,
        )
        self.btn_sample_text.pack(side=tk.LEFT, padx=2)
        ttk.Label(
            review_buttons, text="語音品質檢查：Clean 欄就是 TTS 實際會朗讀的文字",
        ).pack(side=tk.LEFT, padx=8)

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
        repo, token = self._github_settings()
        return GitHubQueueStore(repo, token), repo, token

    def _render_queue(self, queue):
        selected_ids = tuple(self.queue_tree.selection())
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for task in queue.get("tasks", []):
            start = task.get("start_chapter") or 1
            end = task.get("end_chapter") or "全部"
            hf = task.get("hf_progress") or {}
            yt = task.get("youtube_progress") or {}
            observation = self.github_observations.get(task.get("task_id"))
            duplicate_count = task.get("duplicate_chapter_count")
            self.queue_tree.insert("", tk.END, iid=task["task_id"], values=(
                task.get("position"), task.get("book_title"), f"{start}–{end}",
                duplicate_count if duplicate_count is not None else "—", self._queue_status_text(task),
                self._observation_checked_time(observation),
                f"{hf.get('completed', 0)}/{hf.get('total', 0)}",
                f"{yt.get('completed', 0)}/{yt.get('total', 0)}",
                task.get("run_id") or "—",
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
                elif status == "waiting_retry":
                    self.log(f"⏳ {title} 暫停等待安全重試")
                elif status == "needs_attention":
                    self.log(f"⚠ {title} 需要處理：{task.get('reason') or '未知原因'}")
                elif status == "interrupted":
                    self.log(f"🛑 {title} 本次 Run 已中斷；任務已保留，可按「重新排程」")
            self.queue_status_cache[task["task_id"]] = current
        restored_ids = [task_id for task_id in selected_ids if self.queue_tree.exists(task_id)]
        if restored_ids:
            self.queue_tree.selection_set(restored_ids)
            selected_tasks = [
                item for item in queue.get("tasks", []) if item.get("task_id") in restored_ids
            ]
            self._update_queue_control_states(selected_tasks)
            if len(selected_tasks) == 1:
                self._update_selected_task_status(selected_tasks[0])
            else:
                self.selected_status_var.set(f"已選取 {len(selected_tasks)} 筆小說任務；可批次暫停、刪除或調整順位。")
        else:
            self._update_queue_control_states(None)

    def _queue_status_text(self, task):
        status = task.get("status") or "queued"
        # A bound Run's verified GitHub state is authoritative.  Queue-control
        # state must never hide an in-progress (or otherwise verified) Run.
        if task.get("run_id"):
            observation = self.github_observations.get(task.get("task_id"))
            if observation:
                return observation_text(observation)
        return status

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
            for task in self.cloud_queue.get("tasks", []):
                task_id = task.get("task_id")
                if task_id and task.get("run_id") and self.queue_tree.exists(task_id):
                    observation = self.github_observations.get(task_id)
                    self.queue_tree.set(task_id, "status", self._queue_status_text(task))
                    self.queue_tree.set(task_id, "verified", self._observation_checked_time(observation))
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
                monitored = [task for task in queue.get("tasks", []) if task.get("run_id")]
                observations = {}
                with ThreadPoolExecutor(max_workers=min(6, max(1, len(monitored)))) as executor:
                    futures = {
                        executor.submit(
                            self._observe_github_run, repo, token, task["run_id"],
                            self.github_observations.get(task["task_id"]),
                        ): task
                        for task in monitored
                    }
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            observations[task["task_id"]] = future.result()
                        except Exception as error:
                            observations[task["task_id"]] = error_observation("network_error", detail=error)
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
                              observation.get("raw_conclusion") != "success"):
                            conclusion = observation.get("raw_conclusion") or "unknown"
                            terminal_updates.append((
                                task["task_id"], run_id, "needs_attention",
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
                                (item for item in latest.get("tasks", []) if item.get("task_id") == task_id), None
                            )
                            if current and current.get("run_id") == run_id and current.get("status") in BLOCKING_STATES:
                                marker = mark_task_interrupted if target == "interrupted" else mark_task_needs_attention
                                latest = marker(latest, task_id, reason=reason, conclusion=conclusion, ended_at=ended_at)
                        return latest
                    queue = store.mutate(apply_terminal_updates, "Reconcile completed audiobook runs")
                    self._dispatch_queue_workflow()
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
            except Exception as error:
                if self.cloud_queue:
                    for task in self.cloud_queue.get("tasks", []):
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

    def _selected_task(self):
        tasks = self._selected_tasks()
        return tasks[0] if tasks else None

    def _selected_tasks(self):
        selected_ids = set(self.queue_tree.selection())
        if not selected_ids or not self.cloud_queue:
            return []
        return [
            task for task in self.cloud_queue.get("tasks", [])
            if task.get("task_id") in selected_ids
        ]

    def _on_queue_select(self, _event=None):
        tasks = self._selected_tasks()
        if not tasks:
            self._update_queue_control_states(None)
            return
        task = tasks[0]
        if task.get("run_id"):
            self.current_run_id = int(task["run_id"])
            try:
                self.current_repo, self.current_token = self._github_settings()
                self.btn_cancel.config(state=tk.NORMAL if task.get("status") in {"running", "dispatching", "waiting_retry"} else tk.DISABLED)
            except Exception:
                pass
        else:
            self.current_run_id = None
            self.btn_cancel.config(state=tk.DISABLED)
        self._update_queue_control_states(tasks)
        if len(tasks) > 1:
            self.selected_status_var.set(f"已選取 {len(tasks)} 筆小說任務；可批次暫停、刪除或調整順位。")
            return
        self._update_selected_task_status(task)
        if task.get("task_id") != self.editing_task_id:
            self._load_queue_task_for_edit(task)

    def _update_queue_control_states(self, task_or_tasks):
        tasks = task_or_tasks if isinstance(task_or_tasks, (list, tuple)) else ([task_or_tasks] if task_or_tasks else [])
        statuses = {task.get("status") for task in tasks}
        all_toggleable = bool(tasks) and statuses <= {"queued", "paused"}
        all_paused = bool(tasks) and statuses == {"paused"}
        self.btn_toggle_task.config(
            state=tk.NORMAL if all_toggleable else tk.DISABLED,
            text="恢復排程" if all_paused else "暫停排程",
        )
        has_cancellable_run = (
            len(tasks) == 1 and
            statuses != {"canceling"} and
            (
                bool(tasks[0].get("run_id")) or
                statuses <= {"running", "dispatching", "waiting_retry"}
            )
        )
        self.btn_stop_task.config(
            state=tk.NORMAL if has_cancellable_run else tk.DISABLED,
            text="正在取消…" if statuses == {"canceling"} else "取消本次 Run",
        )
        self.btn_sample_text.config(state=tk.NORMAL if len(tasks) == 1 else tk.DISABLED)

    @staticmethod
    def _text_sample_chapters(task, catalog):
        """Return first/lower-middle/last chapters after applying task filters."""
        total = int(catalog.get("total_chapters") or len(catalog.get("chapters") or []))
        start = max(1, int(task.get("start_chapter") or 1))
        end = min(total, int(task.get("end_chapter") or total))
        excluded = {int(value) for value in task.get("excluded_chapters") or []}
        source_indices = [value for value in range(start, end + 1) if value not in excluded]
        if not source_indices:
            raise ValueError("這項任務的章節範圍已全部排除，沒有可抽查的章節。")
        positions = [0, (len(source_indices) - 1) // 2, len(source_indices) - 1]
        labels = ["第一章", "中間章", "最後一章"]
        samples = []
        for label, position in zip(labels, positions):
            source_index = source_indices[position]
            output_index = position + 1 if task.get("renumber_selected") else source_index
            samples.append({
                "label": label,
                "source_index": source_index,
                "output_index": output_index,
                "url": catalog["base_url"] + catalog["chapters"][source_index - 1],
                "catalog_title": catalog["chapter_titles"][source_index - 1],
            })
        return samples

    @staticmethod
    def _build_text_sample(raw_title, raw_body, book_title):
        raw_text = raw_title + "\n\n" + raw_body
        cleaned = clean_text_content(raw_body, raw_title, book_title)
        return raw_text, chunk_text(cleaned, max_length=18)

    def open_text_sample(self):
        task = self._selected_task()
        if not task:
            messagebox.showinfo("抽查文字", "請先單選一本小說。")
            return

        top = tk.Toplevel(self.root)
        top.title(f"Cleaner 文字抽查｜{task.get('book_title') or '待解析'}")
        top.geometry("1120x760")
        top.minsize(820, 520)
        frame = ttk.Frame(top, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        status_var = tk.StringVar(value="正在解析目錄並取得三個取樣章節…")
        ttk.Label(frame, textvariable=status_var, style="Header.TLabel").pack(anchor=tk.W, pady=(0, 8))
        notebook = ttk.Notebook(frame)
        notebook.pack(fill=tk.BOTH, expand=True)
        views = {}
        for label in ("第一章", "中間章", "最後一章"):
            page = ttk.Frame(notebook, padding=6)
            notebook.add(page, text=label)
            info_var = tk.StringVar(value="等待載入…")
            ttk.Label(page, textvariable=info_var).pack(anchor=tk.W, pady=(0, 5))
            panes = ttk.Panedwindow(page, orient=tk.HORIZONTAL)
            panes.pack(fill=tk.BOTH, expand=True)
            raw_frame = ttk.LabelFrame(panes, text="Raw TXT（爬蟲原始輸出）")
            clean_frame = ttk.LabelFrame(panes, text="Clean TXT（TTS 實際輸入）")
            raw_box = scrolledtext.ScrolledText(raw_frame, wrap=tk.WORD, font=("Microsoft JhengHei", 11))
            clean_box = scrolledtext.ScrolledText(clean_frame, wrap=tk.WORD, font=("Microsoft JhengHei", 11))
            raw_box.pack(fill=tk.BOTH, expand=True)
            clean_box.pack(fill=tk.BOTH, expand=True)
            panes.add(raw_frame, weight=1)
            panes.add(clean_frame, weight=1)
            views[label] = (info_var, raw_box, clean_box)

        def show_error(detail):
            status_var.set("抽查失敗")
            messagebox.showerror("抽查文字失敗", detail, parent=top)

        def render(results):
            warnings = 0
            for sample, raw_text, clean_text in results:
                info_var, raw_box, clean_box = views[sample["label"]]
                raw_chars = len(raw_text.strip())
                clean_chars = len(clean_text.replace("\n", "").strip())
                removed = max(0, raw_chars - clean_chars)
                ratio = (removed / raw_chars * 100) if raw_chars else 0
                warning = ""
                if not clean_text.strip():
                    warning = "　⚠ Clean 為空"
                elif ratio >= 50:
                    warning = "　⚠ 清除比例偏高"
                suspicious = [word for word in ("本站", "域名", "最新地址", "手機閱讀", "廣告") if word in clean_text]
                if suspicious:
                    warning += f"　⚠ 疑似殘留：{'、'.join(suspicious)}"
                warnings += bool(warning)
                mapping = f"來源第 {sample['source_index']} 章"
                if sample["output_index"] != sample["source_index"]:
                    mapping += f" → 輸出第 {sample['output_index']} 章"
                info_var.set(
                    f"{mapping}｜{sample['catalog_title']}｜Raw {raw_chars:,} 字｜Clean {clean_chars:,} 字｜約清除 {ratio:.1f}%{warning}"
                )
                for box, content in ((raw_box, raw_text), (clean_box, clean_text)):
                    box.delete("1.0", tk.END)
                    box.insert("1.0", content)
                    box.config(state=tk.DISABLED)
            status_var.set(f"抽查完成：3 個位置，{warnings} 個需要留意。左右內容可直接捲動比對。")

        def worker():
            try:
                catalog = parse_catalog(task.get("catalog_url") or "")
                samples = self._text_sample_chapters(task, catalog)
                results = []
                for sample in samples:
                    title, body = fetch_chapter_text(sample["url"])
                    raw_text, clean_text = self._build_text_sample(
                        title, body, task.get("book_title") or catalog.get("book_title") or "",
                    )
                    results.append((sample, raw_text, clean_text))
                self.root.after(0, lambda: render(results) if top.winfo_exists() else None)
            except Exception as error:
                self.root.after(0, lambda detail=str(error): show_error(detail) if top.winfo_exists() else None)
        threading.Thread(target=worker, daemon=True).start()

    def _update_selected_task_status(self, task):
        hf = task.get("hf_progress") or {}
        yt = task.get("youtube_progress") or {}
        run_text = task.get("run_id") or "尚未建立"
        extra = ""
        if task.get("requeue_after_edit"):
            extra = "\n章節設定已更新；正在停止舊 Run，確認停止後會自動重新排程。"
        self.selected_status_var.set(
            f"《{task.get('book_title') or '待解析'}》｜第 {task.get('start_chapter') or 1}～{task.get('end_chapter') or '最後'} 章\n"
            f"狀態：{self._queue_status_text(task)}　｜　GitHub Run：{run_text}\n"
            f"HF：{hf.get('completed', 0)}/{hf.get('total', 0)}　｜　YouTube：{yt.get('completed', 0)}/{yt.get('total', 0)}{extra}"
        )

    def reset_chapter_editor(self):
        self.catalog_load_token += 1
        self.editing_task_id = None
        self.catalog_data = None
        self.excluded_chapters.clear()
        self.renumber_selected_chapters = False
        self.queue_tree.selection_remove(*self.queue_tree.selection())
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
                self.root.after(0, lambda: self._finish_queue_task_load(token, task["task_id"], result))
            except Exception as error:
                self.root.after(0, lambda detail=str(error): self._on_parse_failed(detail))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_queue_task_load(self, token, task_id, result):
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

    def open_selected_task_progress(self, _event=None):
        task = self._selected_task()
        if not task:
            messagebox.showinfo("查看進度", "請先選取一個小說任務。")
            return
        self._open_task_progress(task)

    def _open_task_progress(self, task):
        task_id = task["task_id"]
        existing = self.task_progress_windows.get(task_id)
        if existing and existing["top"].winfo_exists():
            existing["top"].deiconify()
            existing["top"].lift()
            existing["top"].focus_force()
            return

        top = tk.Toplevel(self.root)
        top.title(f"{task.get('book_title') or '小說任務'}｜Run {task.get('run_id') or '等待建立'}")
        top.geometry("900x680")
        top.minsize(720, 480)

        frame = ttk.Frame(top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        title_var = tk.StringVar(value=f"📖 {task.get('book_title') or '待解析'}")
        ttk.Label(frame, textvariable=title_var, style="Title.TLabel").pack(anchor=tk.W)
        summary_var = tk.StringVar(value="正在取得雲端狀態…")
        ttk.Label(frame, textvariable=summary_var, style="Header.TLabel").pack(anchor=tk.W, pady=(5, 8))

        log_box = scrolledtext.ScrolledText(
            frame, background="#1e1e1e", foreground="#dcdcdc", font=("Consolas", 10), wrap=tk.WORD
        )
        log_box.pack(fill=tk.BOTH, expand=True)
        log_box.tag_config("hyperlink", foreground="#4da6ff", underline=1)
        log_box.tag_config("success", foreground="#44bd32")
        log_box.tag_config("warning", foreground="#e1b12c")
        log_box.tag_config("error", foreground="#e84118")
        log_box.tag_bind("hyperlink", "<Enter>", lambda _event: log_box.config(cursor="hand2"))
        log_box.tag_bind("hyperlink", "<Leave>", lambda _event: log_box.config(cursor=""))
        close_event = threading.Event()

        state = {
            "top": top, "task": dict(task), "title_var": title_var, "summary_var": summary_var,
            "log": log_box, "close_event": close_event, "seen": set(), "job_states": {},
            "completed_logs": set(), "last_log_check": {}, "run_announced": None, "link_counter": 0,
        }
        self.task_progress_windows[task_id] = state

        def close_window():
            close_event.set()
            self.task_progress_windows.pop(task_id, None)
            if top.winfo_exists():
                top.destroy()

        top.protocol("WM_DELETE_WINDOW", close_window)
        ttk.Button(frame, text="關閉視窗（不停止雲端任務）", command=close_window).pack(anchor=tk.E, pady=(8, 0))

        self._append_task_log(task_id, f"🚀 任務：{task.get('book_title') or '待解析'}")
        self._append_task_log(
            task_id,
            f"📚 製作範圍：第 {task.get('start_chapter') or 1}～{task.get('end_chapter') or '全部'} 章",
        )
        if task.get("excluded_chapters"):
            self._append_task_log(task_id, f"🚫 排除章節：{', '.join(map(str, task['excluded_chapters']))}")
        if not task.get("run_id"):
            self._append_task_log(task_id, "⏳ 已加入雲端佇列，正在等待前一個任務完成…", "warning")
        threading.Thread(target=self._poll_task_progress, args=(task_id,), daemon=True).start()

    def _append_task_log(self, task_id, message, style=None, url=None):
        state = self.task_progress_windows.get(task_id)
        if not state or state["close_event"].is_set() or not state["top"].winfo_exists():
            return
        box = state["log"]
        timestamp = time.strftime("%H:%M:%S")
        box.insert(tk.END, f"[{timestamp}] ")
        if url:
            box.insert(tk.END, message + "\n")
            state["link_counter"] += 1
            tag = f"url_{state['link_counter']}"
            box.insert(tk.END, url, ("hyperlink", tag))
            box.tag_bind(tag, "<Button-1>", lambda _event, target=url: webbrowser.open(target))
            box.insert(tk.END, "\n")
        else:
            box.insert(tk.END, message + "\n", (style,) if style else ())
        box.see(tk.END)

    def _task_from_current_queue(self, task_id, fallback):
        if self.cloud_queue:
            current = next((item for item in self.cloud_queue.get("tasks", []) if item.get("task_id") == task_id), None)
            if current:
                return dict(current)
        return dict(fallback)

    @staticmethod
    def _job_display_status(job):
        status = job.get("status")
        conclusion = job.get("conclusion")
        if status == "completed":
            return {
                "success": "✅ 完成", "failure": "❌ 失敗", "cancelled": "🛑 已取消",
                "skipped": "⏭️ 已略過",
            }.get(conclusion, f"完成（{conclusion or '未知'}）")
        return {"queued": "⏳ 等待中", "in_progress": "⚡ 執行中"}.get(status, status or "未知")

    def _apply_task_snapshot(self, task_id, task, run_data, jobs, marker_events):
        state = self.task_progress_windows.get(task_id)
        if not state or state["close_event"].is_set() or not state["top"].winfo_exists():
            return
        state["task"] = dict(task)
        run_id = run_data.get("id")
        run_url = run_data.get("html_url")
        if state["run_announced"] != run_id:
            state["run_announced"] = run_id
            state["top"].title(f"{task.get('book_title') or '小說任務'}｜Run {run_id}")
            self._append_task_log(task_id, f"🔗 GitHub Run #{run_id}（點擊開啟）", url=run_url)
            self._append_task_log(task_id, f"↻ 目前雲端執行輪次：Run attempt {run_data.get('run_attempt', 1)}")

        setup_jobs = [job for job in jobs if "Parse Catalog" in (job.get("name") or "")]
        worker_jobs = [job for job in jobs if re.search(r"Worker\s+\d+", job.get("name") or "", re.I)]
        completed_workers = sum(
            1 for job in worker_jobs if job.get("status") == "completed" and job.get("conclusion") == "success"
        )
        hf = task.get("hf_progress") or {}
        youtube = task.get("youtube_progress") or {}
        run_status = run_data.get("conclusion") or run_data.get("status") or "未知"
        state["summary_var"].set(
            f"狀態：{run_status}　｜　Workers：{completed_workers}/{len(worker_jobs)}　｜　"
            f"HF：{hf.get('completed', 0)}/{hf.get('total', 0)}　｜　"
            f"YouTube：{youtube.get('completed', 0)}/{youtube.get('total', 0)}"
        )

        ordered_jobs = setup_jobs + worker_jobs + [
            job for job in jobs if job not in setup_jobs and job not in worker_jobs and job.get("conclusion") != "skipped"
        ]
        for job in ordered_jobs:
            name = job.get("name") or "未命名 Job"
            key = (job.get("status"), job.get("conclusion"))
            if state["job_states"].get(name) == key:
                continue
            state["job_states"][name] = key
            display = self._job_display_status(job)
            if "Parse Catalog" in name:
                if job.get("status") == "in_progress":
                    message = "🔍 正在解析小說目錄並建立 Worker 分工…"
                elif job.get("conclusion") == "success":
                    message = f"✅ 目錄解析完成，已建立 {len(worker_jobs)} 個 Worker"
                else:
                    message = f"🔍 目錄解析：{display}"
            else:
                message = f"{name}｜{display}"
            style = "error" if job.get("conclusion") == "failure" else "success" if job.get("conclusion") == "success" else None
            self._append_task_log(task_id, message, style)

            if "Parse Catalog" in name:
                for step in job.get("steps") or []:
                    step_name = step.get("name") or ""
                    if not re.search(r"parse|catalog|matrix|目錄|解析", step_name, re.I):
                        continue
                    step_key = f"setup-step:{step_name}"
                    step_state = (step.get("status"), step.get("conclusion"))
                    if state["job_states"].get(step_key) == step_state:
                        continue
                    state["job_states"][step_key] = step_state
                    self._append_task_log(
                        task_id, f"   └─ {step_name}｜{self._job_display_status(step)}",
                        "error" if step.get("conclusion") == "failure" else
                        "success" if step.get("conclusion") == "success" else None,
                    )

        for marker_key, message, style in marker_events:
            if marker_key not in state["seen"]:
                state["seen"].add(marker_key)
                self._append_task_log(task_id, message, style)

        terminal = run_data.get("status") == "completed"
        terminal_key = ("run_terminal", run_data.get("conclusion"))
        if terminal and terminal_key not in state["seen"]:
            state["seen"].add(terminal_key)
            conclusion = run_data.get("conclusion")
            if conclusion == "success":
                self._append_task_log(task_id, "🎉 雲端任務已全部完成。", "success")
            else:
                self._append_task_log(task_id, f"❌ 雲端任務結束：{conclusion or '未知結果'}", "error")

    @staticmethod
    def _parse_task_log_markers(text, job_id):
        events = []
        for worker, chapters, progress in re.findall(
            r"\[PROGRESS_MARKER\] Worker-(\d+) \| Ch (\S+) (?:complete|done) \((\d+/\d+)\)", text
        ):
            events.append((f"{job_id}:worker:{worker}:{chapters}:{progress}",
                           f"⚡ Worker {worker}｜第 {chapters} 章｜✅ 合成完成（進度 {progress}）", "success"))
        for action, part, chapters, detail in re.findall(
            r"\[API_UPLOAD_MARKER\] (START|DONE) \| Part (\S+) \| Ch (\S+) \| (.+)", text
        ):
            icon = "▶️" if action == "START" else "✅"
            label = "開始上傳" if action == "START" else "上傳完成並加入播放清單"
            events.append((f"{job_id}:youtube:{action}:{part}:{chapters}",
                           f"📤 YouTube Part {part}｜第 {chapters} 章｜{icon} {label}（{detail.strip()}）",
                           "success" if action == "DONE" else None))
        for part in re.findall(r"\[HF_ARCHIVE_MARKER\] DONE \| Part (\d+)", text):
            events.append((f"{job_id}:hf:{part}", f"📦 Hugging Face Part {part}｜✅ 備份完成", "success"))
        for summary_text in re.findall(r"\[RUN_SUMMARY\] (\{[^\r\n]+\})", text):
            try:
                summary = json.loads(summary_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if summary.get("kind") == "worker":
                message = f"📋 Worker {summary.get('worker')} 摘要｜完成 {summary.get('completed')}/{summary.get('total')} 章"
            else:
                message = f"📋 YouTube 摘要｜{summary.get('status')}｜完成 {summary.get('completed')}/{summary.get('total')} Parts"
            events.append((f"{job_id}:summary:{summary_text}", message, None))
        return events

    def _poll_task_progress(self, task_id):
        state = self.task_progress_windows.get(task_id)
        if not state:
            return
        close_event = state["close_event"]
        try:
            repo, token = self._github_settings()
            headers = {
                "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            while not close_event.is_set():
                task = self._task_from_current_queue(task_id, state["task"])
                run_id = task.get("run_id")
                if not run_id:
                    try:
                        store, _, _ = self._queue_store()
                        queue, _ = store.load()
                        task = next((item for item in queue.get("tasks", []) if item.get("task_id") == task_id), task)
                    except Exception:
                        pass
                    run_id = task.get("run_id")
                    if not run_id:
                        self.root.after(0, lambda tid=task_id, t=dict(task): self._update_waiting_task_summary(tid, t))
                        close_event.wait(10)
                        continue

                run_response = requests.get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}", headers=headers, timeout=15)
                run_response.raise_for_status()
                jobs_response = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", headers=headers, timeout=15
                )
                jobs_response.raise_for_status()
                run_data = run_response.json()
                jobs = jobs_response.json().get("jobs", [])
                marker_events = []
                # Paint the lightweight Run/Jobs snapshot immediately.  Log
                # downloads can be large and must never hold the whole window
                # on "正在取得雲端狀態".
                self.root.after(
                    0, lambda tid=task_id, t=dict(task), rd=run_data, js=jobs:
                    self._apply_task_snapshot(tid, t, rd, js, [])
                )
                now = time.time()
                for job in jobs:
                    job_id = job.get("id")
                    job_status = job.get("status")
                    # Completed jobs are already fully represented by the Jobs
                    # API.  Fetch only live logs for progress markers; a
                    # cancelled 20-worker Run therefore opens immediately.
                    if not job_id or job_status != "in_progress":
                        continue
                    if now - state["last_log_check"].get(job_id, 0) < 30:
                        continue
                    state["last_log_check"][job_id] = now
                    try:
                        log_response = requests.get(
                            f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs",
                            headers=headers, timeout=12, allow_redirects=True,
                        )
                        if log_response.status_code == 200:
                            marker_events.extend(self._parse_task_log_markers(log_response.text, job_id))
                    except requests.RequestException:
                        pass
                if marker_events:
                    self.root.after(
                        0, lambda tid=task_id, t=dict(task), rd=run_data, js=jobs, me=marker_events:
                        self._apply_task_snapshot(tid, t, rd, js, me)
                    )
                if run_data.get("status") == "completed":
                    break
                close_event.wait(10)
        except Exception as error:
            self.root.after(0, lambda tid=task_id, detail=str(error): self._task_progress_error(tid, detail))
            if not close_event.wait(10):
                threading.Thread(target=self._poll_task_progress, args=(task_id,), daemon=True).start()

    def _update_waiting_task_summary(self, task_id, task):
        state = self.task_progress_windows.get(task_id)
        if state and state["top"].winfo_exists():
            state["task"] = dict(task)
            state["summary_var"].set(f"狀態：{task.get('status') or 'queued'}｜等待建立 GitHub Run")

    def _task_progress_error(self, task_id, detail):
        state = self.task_progress_windows.get(task_id)
        if state and state["top"].winfo_exists():
            state["summary_var"].set("暫時無法取得 GitHub 狀態；雲端任務不受影響")
            self._append_task_log(task_id, f"⚠ GitHub 狀態同步失敗：{detail}", "warning")

    def _mutate_queue_async(self, callback, message, success_message=None):
        def worker():
            try:
                store, _, _ = self._queue_store()
                queue = store.mutate(callback, message)
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                try:
                    self._dispatch_queue_workflow()
                except Exception as dispatch_error:
                    self.root.after(0, lambda e=str(dispatch_error): self.log(
                        f"⚠ 佇列已更新，但調度器暫時無法啟動：{e}"
                    ))
                if success_message:
                    self.root.after(0, lambda m=success_message: self.log(m))
            except Exception as error:
                def show_error(detail=str(error)):
                    self._update_queue_control_states(self._selected_task())
                    messagebox.showerror("雲端佇列", detail)
                self.root.after(0, show_error)
        threading.Thread(target=worker, daemon=True).start()

    def _dispatch_queue_workflow(self):
        _, repo, token = self._queue_store()
        response = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/queue-dispatcher.yml/dispatches",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": "master"}, timeout=15,
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(f"無法啟動雲端調度器 ({response.status_code}): {response.text}")

    def trigger_queue_dispatcher(self):
        def worker():
            try:
                self._dispatch_queue_workflow()
                self.root.after(0, lambda: self.log("✓ 已啟動一次雲端調度檢查"))
            except Exception as error:
                self.root.after(0, lambda e=str(error): messagebox.showerror("調度器", e))
        threading.Thread(target=worker, daemon=True).start()

    def enqueue_current_task(self):
        if not self.catalog_data:
            messagebox.showwarning("提示", "請先解析小說目錄")
            return
        try:
            start = int(self.entry_start.get())
            end = int(self.entry_end.get())
        except ValueError:
            messagebox.showwarning("提示", "章節範圍必須是數字")
            return
        task = new_task(
            self.url_entry.get().strip(), self.catalog_data.get("book_title", ""), start, end,
            sorted(self.excluded_chapters), self.renumber_selected_chapters,
            self.catalog_data.get("duplicate_chapter_count"),
        )
        self._mutate_queue_async(
            lambda queue: add_tasks(queue, [task]),
            f"Add {task['book_title']} to audiobook queue",
            f"✓ 已加入任務：{task['book_title']}（第 {start}～{end} 章）",
        )

    def update_selected_task_chapters(self):
        task = self._selected_task()
        if not task or task.get("task_id") != self.editing_task_id:
            messagebox.showinfo("更新章節", "請先從上方雲端小說佇列選取要編輯的小說。")
            return
        try:
            start, end = int(self.entry_start.get()), int(self.entry_end.get())
            total = int((self.catalog_data or {}).get("total_chapters") or 0)
            if start < 1 or end < start or (total and end > total):
                raise ValueError
        except ValueError:
            messagebox.showwarning("更新章節", "請輸入有效且不超過全書章數的章節範圍。")
            return
        active = task.get("status") in {"running", "dispatching", "canceling"}
        task_id, run_id = task["task_id"], task.get("run_id")
        excluded = sorted(value for value in self.excluded_chapters if start <= value <= end)
        original_button_text = self.btn_update_queue.cget("text")
        self.btn_update_queue.config(state=tk.DISABLED, text="⏳ 正在更新雲端佇列…")
        self.selected_status_var.set(
            f"《{task.get('book_title') or '待解析'}》｜正在儲存第 {start}～{end} 章設定…"
        )

        def finish_update(queue, dispatch_error=None):
            self._render_queue(queue)
            self.btn_update_queue.config(state=tk.NORMAL, text=original_button_text)
            updated = next((item for item in queue.get("tasks", []) if item.get("task_id") == task_id), None)
            if updated:
                self._update_selected_task_status(updated)
            result = "已停止舊 Run，確認取消後自動重新排程" if active else "已更新雲端佇列"
            self.log(f"✓ {task.get('book_title')}：第 {start}～{end} 章；{result}")
            if dispatch_error:
                self.log(f"⚠ 章節設定已儲存，但調度器暫時無法啟動：{dispatch_error}")

        def fail_update(detail):
            self.btn_update_queue.config(state=tk.NORMAL, text=original_button_text)
            self._update_selected_task_status(task)
            messagebox.showerror("更新章節失敗", detail)

        def worker():
            try:
                store, repo, token = self._queue_store()
                if active and run_id:
                    response = requests.post(
                        f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel",
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15,
                    )
                    if response.status_code not in (200, 202, 409):
                        raise RuntimeError(f"取消舊 Run 失敗 ({response.status_code}): {response.text}")
                queue = store.mutate(
                    lambda value: update_task_chapters(
                        value, task_id, start, end, excluded, active,
                        self.renumber_selected_chapters,
                        (self.catalog_data or {}).get("duplicate_chapter_count"),
                    ),
                    f"Update chapter plan for audiobook task {task_id}",
                )
                self.cloud_queue = queue
                dispatch_error = None
                try:
                    self._dispatch_queue_workflow()
                except Exception as error:
                    dispatch_error = str(error)
                self.root.after(0, lambda q=queue, e=dispatch_error: finish_update(q, e))
            except Exception as error:
                self.root.after(0, lambda detail=str(error): fail_update(detail))
        threading.Thread(target=worker, daemon=True).start()

    def open_batch_queue_dialog(self):
        top = tk.Toplevel(self.root)
        top.title("批量加入小說網址")
        top.geometry("620x380")
        ttk.Label(top, text="每行貼上一個小說目錄網址；系統會依行序解析並加入佇列。全部預設製作第 1 章到全書。") .pack(anchor=tk.W, padx=10, pady=8)
        text_box = scrolledtext.ScrolledText(top, height=14)
        text_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        def submit():
            urls = [line.strip() for line in text_box.get("1.0", tk.END).splitlines() if line.strip()]
            if not urls:
                return
            top.destroy()
            self.log(f"正在解析並加入 {len(urls)} 部小說…")
            def worker():
                tasks = []
                try:
                    for url in urls:
                        result = parse_catalog(url)
                        if not result.get("success"):
                            raise RuntimeError(f"解析失敗：{url}：{result.get('error')}")
                        tasks.append(new_task(
                            url, result["book_title"], 1, result["total_chapters"],
                            duplicate_chapter_count=result.get("duplicate_chapter_count"),
                        ))
                    store, _, _ = self._queue_store()
                    queue = store.mutate(lambda value: add_tasks(value, tasks), f"Batch add {len(tasks)} audiobook tasks")
                    self.cloud_queue = queue
                    self.root.after(0, lambda: self._render_queue(queue))
                    self._dispatch_queue_workflow()
                    self.root.after(0, lambda: self.log(f"✓ 已依順序加入 {len(tasks)} 部小說"))
                except Exception as error:
                    self.root.after(0, lambda e=str(error): messagebox.showerror("批量加入失敗", e))
            threading.Thread(target=worker, daemon=True).start()
        ttk.Button(top, text="依順序加入", style="Accent.TButton", command=submit).pack(pady=8)

    def move_selected_task(self, delta):
        tasks = self._selected_tasks()
        if tasks:
            task_ids = [task["task_id"] for task in tasks]
            self._mutate_queue_async(
                lambda queue: move_tasks(queue, task_ids, delta),
                f"Move {len(task_ids)} audiobook task(s)",
            )

    def toggle_selected_task(self):
        tasks = self._selected_tasks()
        if not tasks:
            messagebox.showinfo("暫停／恢復", "請先選取一筆或多筆小說任務。")
            return
        if any(task.get("status") not in {"queued", "paused"} for task in tasks):
            messagebox.showinfo("暫停／恢復", "只有等待中的任務可以暫停；執行中的任務請使用「取消本次 Run」。")
            return
        new_status = "queued" if all(task.get("status") == "paused" for task in tasks) else "paused"
        action = "恢復排程" if new_status == "queued" else "暫停排程"
        self.btn_toggle_task.config(state=tk.DISABLED, text=f"正在{action}…")
        self.selected_status_var.set(f"{len(tasks)} 筆小說任務｜正在{action}…")
        task_ids = [task["task_id"] for task in tasks]

        def mutate(queue):
            for task_id in task_ids:
                queue = update_task(queue, task_id, status=new_status)
            return queue

        self._mutate_queue_async(
            mutate,
            f"Set {len(task_ids)} audiobook task(s) to {new_status}",
            f"✓ 已將 {len(task_ids)} 筆小說任務{action}",
        )

    def _find_active_task_id(self, exclude_task_id=None):
        for other in (self.cloud_queue or {}).get("tasks", []):
            other_id = other.get("task_id")
            if other_id and other_id != exclude_task_id:
                obs = self.github_observations.get(other_id) or {}
                if (
                    (obs.get("kind") == "ok" and obs.get("raw_status") != "completed") or
                    is_task_active(other)
                ):
                    return other_id
        return None

    def requeue_selected_task(self):
        task = self._selected_task()
        if not task:
            return
        title = task.get("book_title") or "小說任務"
        run_id = task.get("run_id")
        observation = self.github_observations.get(task.get("task_id")) or {}
        github_active = (
            run_id and observation.get("kind") == "ok" and
            observation.get("raw_status") != "completed"
        )
        # If GitHub could not be verified, retain the conservative local guard
        # so an active Run is not orphaned and duplicated.
        active = github_active or (
            run_id and observation.get("kind") != "ok" and
            task.get("status") in {"running", "dispatching", "waiting_retry", "canceling"}
        )
        active_task_id = self._find_active_task_id(exclude_task_id=task.get("task_id"))
        if active:
            if not messagebox.askyesno(
                    "確認重新排程",
                    f"「{title}」目前的 Run 仍在 GitHub 執行。\n"
                    "要先取消本次 Run，並在確認結束後自動重新排程嗎？"):
                return
            self.selected_status_var.set(f"《{title}》｜正在取消目前 Run，之後會自動重新排程…")

            def worker():
                try:
                    store, repo, token = self._queue_store()
                    run_already_ended = False
                    try:
                        response = requests.post(
                            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                            timeout=15,
                        )
                        if response.status_code == 409:
                            run_already_ended = True
                        elif response.status_code not in (200, 202):
                            raise RuntimeError(f"取消 Run 失敗 ({response.status_code}): {response.text}")
                    except Exception as cancel_err:
                        if not isinstance(cancel_err, RuntimeError):
                            run_already_ended = True
                        else:
                            raise

                    if run_already_ended:
                        def requeue_finished(value):
                            value = update_task(
                                value, task["task_id"], status="needs_attention",
                                reason="user_requeue_after_run_ended",
                            )
                            return requeue_task_after_active(value, task["task_id"], active_id=active_task_id)
                        queue = store.mutate(
                            requeue_finished,
                            f"Requeue completed audiobook task {task['task_id']}",
                        )
                    else:
                        queue = store.mutate(
                            lambda value: update_task(
                                value, task["task_id"], status="canceling",
                                reason="user_requeue", requeue_after_edit=True,
                            ),
                            f"Cancel and requeue audiobook task {task['task_id']}",
                        )
                    self.cloud_queue = queue
                    self.root.after(0, lambda: self._render_queue(queue))
                    result_message = (
                        f"✓ {title} 的 Run 已結束，已直接重新排程。"
                        if run_already_ended else
                        f"✓ {title} 的 Run 取消要求已送出；GitHub 確認結束後會自動重新排程。"
                    )
                    self.root.after(0, lambda message=result_message: self.log(message))
                    try:
                        self._dispatch_queue_workflow()
                    except Exception as dispatch_error:
                        self.root.after(0, lambda e=str(dispatch_error): self.log(
                            f"⚠ 重新排程已完成，但調度器暫時無法啟動：{e}"
                        ))
                except Exception as error:
                    self.root.after(0, lambda e=str(error): messagebox.showerror("重新排程失敗", e))
            threading.Thread(target=worker, daemon=True).start()
            return
        self._mutate_queue_async(
            lambda queue: requeue_task_after_active(queue, task["task_id"], active_id=active_task_id),
            f"Requeue audiobook task {task['task_id']}",
            f"✓ {title} 已排到目前執行中小說的下一順位",
        )

    def stop_selected_task(self):
        task = self._selected_task()
        if not task:
            messagebox.showinfo("取消本次 Run", "請先選取一筆小說任務。")
            return
        run_id = task.get("run_id")
        if not (run_id or task.get("status") in {"running", "dispatching", "waiting_retry", "canceling"}):
            messagebox.showinfo("取消本次 Run", "這筆任務目前沒有可取消的 Run。")
            return
        title = task.get("book_title") or "小說任務"
        if not messagebox.askyesno("確認取消", f"要取消「{title}」目前這一次 Run 嗎？\n任務會保留，之後可以重新排程。"):
            return
        self.btn_stop_task.config(state=tk.DISABLED, text="正在送出取消…")
        self.selected_status_var.set(f"《{title}》｜正在向 GitHub 送出取消要求…")
        self.log(f"🛑 正在取消 {title} 的 Run {run_id or '（尚未建立）'}…")

        def fail_stop(detail):
            current = self._selected_task()
            self._update_queue_control_states(current)
            if current:
                self._update_selected_task_status(current)
            messagebox.showerror("取消 Run 失敗", detail)

        def worker():
            try:
                store, repo, token = self._queue_store()
                if run_id:
                    try:
                        response = requests.post(
                            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15,
                        )
                        if response.status_code not in (200, 202, 409):
                            raise RuntimeError(f"取消 Run 失敗 ({response.status_code}): {response.text}")
                    except Exception as cancel_error:
                        if isinstance(cancel_error, RuntimeError):
                            raise
                        logging.warning(f"Could not cancel run {run_id}: {cancel_error}")
                target_status = "stopped" if task.get("status") == "waiting_retry" or not run_id else "canceling"
                queue = store.mutate(lambda value: update_task(value, task["task_id"], status=target_status, reason="user_cancelled"), f"Stop audiobook task {task['task_id']}")
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                self.root.after(0, lambda: self.log(
                    f"✓ GitHub 已接受 {title} 的取消要求；正在等待 Run 停止。"
                    if run_id else f"✓ {title} 已停止，不會建立新的 Run。"
                ))
                try:
                    self._dispatch_queue_workflow()
                except Exception as dispatch_error:
                    self.root.after(0, lambda e=str(dispatch_error): self.log(
                        f"⚠ 取消要求已完成，但調度器暫時無法啟動：{e}"
                    ))
            except Exception as error:
                self.root.after(0, lambda e=str(error): fail_stop(e))
        threading.Thread(target=worker, daemon=True).start()

    def delete_selected_task(self):
        tasks = self._selected_tasks()
        if not tasks or not messagebox.askyesno("確認刪除", f"刪除選取的 {len(tasks)} 筆佇列任務？\n執行中的 Run 會送出取消；不會刪除既有 HF 或 YouTube 成品。"):
            return
        task_ids = [task["task_id"] for task in tasks]
        def worker():
            try:
                store, repo, token = self._queue_store()
                for task in tasks:
                    run_id = task.get("run_id")
                    if run_id and task.get("status") in {"running", "dispatching", "waiting_retry", "canceling"}:
                        requests.post(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel", headers={"Authorization": f"Bearer {token}"}, timeout=15)

                def mutate(value):
                    for task_id in task_ids:
                        value = delete_task(value, task_id)
                    return value

                queue = store.mutate(mutate, f"Delete {len(task_ids)} audiobook task(s)")
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                self._dispatch_queue_workflow()
            except Exception as error:
                self.root.after(0, lambda e=str(error): messagebox.showerror("刪除失敗", e))
        threading.Thread(target=worker, daemon=True).start()

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        prefix = f"[{timestamp}] "
        self.log_text.insert(tk.END, prefix)
        
        # 尋找網址並加入超連結標籤
        url_pattern = re.compile(r'(https?://\S+)')
        parts = url_pattern.split(message)
        for part in parts:
            if url_pattern.match(part):
                self.link_counter += 1
                tag_name = f"link_{self.link_counter}"
                self.log_text.insert(tk.END, part, (tag_name, "hyperlink"))
                self.log_text.tag_bind(tag_name, "<Button-1>", lambda e, u=part: webbrowser.open(u))
            else:
                self.log_text.insert(tk.END, part)
                
        self.log_text.insert(tk.END, "\n")
        self.log_text.see(tk.END)

    # ── 解析目錄 ──
    def start_parse_catalog(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("提示", "請輸入小說目錄網址！")
            return

        self.btn_parse.config(state=tk.DISABLED)
        self.lbl_status.config(text="解析目錄中...", foreground="#e1b12c")
        self.log(f"正在解析網址: {url} ...")

        def _worker():
            try:
                res = parse_catalog(url)
                self.root.after(0, lambda: self._on_parse_success(res))
            except Exception as e:
                error_message = str(e)
                self.root.after(0, lambda message=error_message: self._on_parse_failed(message))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_parse_success(self, res):
        self.btn_parse.config(state=tk.NORMAL)
        if res and res.get("success"):
            self.catalog_data = res
            book_title = res["book_title"]
            total = res["total_chapters"]
            duplicate_count = int(res.get("duplicate_chapter_count") or 0)

            self.lbl_book_info.config(
                text=f"書名: {book_title} | 總章節: {total} 章 | 重複章節: {duplicate_count} 章",
                foreground="#27ae60",
            )
            self.entry_start.delete(0, tk.END)
            self.entry_start.insert(0, "1")
            self.entry_end.delete(0, tk.END)
            self.entry_end.insert(0, str(total))
            
            self.btn_filter.config(state=tk.NORMAL)
            self.excluded_chapters.clear()
            self.renumber_selected_chapters = False
            self._update_chapter_selection_summary(1, total)

            self.lbl_status.config(text="解析完成", foreground="#27ae60")
            self.log(f"✓ 解析成功！書名:【{book_title}】，共找到 {total} 章節；標記 {duplicate_count} 個重複章節。")
        else:
            self._on_parse_failed("無法找到章節內容")

    def _on_parse_failed(self, error_msg):
        self.btn_parse.config(state=tk.NORMAL)
        self.btn_filter.config(state=tk.DISABLED)
        self.lbl_status.config(text="解析失敗", foreground="#e74c3c")
        self.log(f"✗ 目錄解析失敗: {error_msg}")
        messagebox.showerror("解析失敗", f"無法讀取該目錄網址：\n{error_msg}")

    def _open_chapter_filter_dialog(self):
        if not self.catalog_data:
            return
            
        titles = self.catalog_data.get("chapter_titles", [])
        duplicate_use_number = tk.BooleanVar(value=True)
        duplicate_use_name = tk.BooleanVar(value=True)
        duplicate_indices = set()
        if not titles:
            messagebox.showinfo("提示", "目前沒有章節標題資訊可供篩選。")
            return
            
        try:
            cur_start = int(self.entry_start.get().strip())
            cur_end = int(self.entry_end.get().strip())
        except ValueError:
            cur_start, cur_end = 1, len(titles)
            
        total_chapters = len(titles)
        cur_start = max(1, min(cur_start, total_chapters))
        cur_end = max(1, min(cur_end, total_chapters))
        
        top = tk.Toplevel(self.root)
        top.title("選擇要轉換的章節")
        top.geometry("1080x620")
        top.minsize(900, 400)
        top.transient(self.root)
        top.grab_set()

        BG_COLOR = "#f5f6fa"
        top.configure(bg=BG_COLOR)

        header_frame = ttk.Frame(top)
        header_frame.pack(fill=tk.X, padx=15, pady=(15, 5))

        ttk.Label(header_frame, text="請取消勾選「不想轉換」的章節 (點擊列或按空白鍵切換勾選)", font=("Microsoft JhengHei", 10, "bold")).pack(anchor="w")

        info_lbl = ttk.Label(header_frame, text=f"全書共 {total_chapters} 章 | 目前範圍：第 {cur_start} ~ {cur_end} 章", font=("Microsoft JhengHei", 9), foreground="#666666")
        info_lbl.pack(anchor="w", pady=(2, 5))

        # 控制列（全書切換 & 搜尋框）
        control_frame = ttk.Frame(top)
        control_frame.pack(fill=tk.X, padx=15, pady=5)

        show_all_var = tk.BooleanVar(value=True)
        
        ttk.Label(control_frame, text="🔍 搜尋:").pack(side=tk.LEFT, padx=(0, 4))
        search_entry = ttk.Entry(control_frame, width=16)
        search_entry.pack(side=tk.LEFT, padx=(0, 10))

        # 中間章節表格 + Scrollbar 區塊
        container = ttk.Frame(top)
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        columns = ("output_number", "normalized_number", "display_number", "chapter_name", "duplicate")
        chapter_tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        headings = {
            "output_number": "編號章節數",
            "normalized_number": "網站章節數正規化",
            "display_number": "網站顯示章節數",
            "chapter_name": "章節名稱",
            "duplicate": "重複標記",
        }
        widths = {
            "output_number": 105, "normalized_number": 155, "display_number": 145,
            "chapter_name": 280, "duplicate": 85,
        }
        for column in columns:
            chapter_tree.heading(column, text=headings[column])
            chapter_tree.column(
                column, width=widths[column], minwidth=70,
                anchor=tk.W if column == "chapter_name" else tk.CENTER,
                stretch=column == "chapter_name",
            )
        y_scrollbar = ttk.Scrollbar(container, orient="vertical", command=chapter_tree.yview)
        x_scrollbar = ttk.Scrollbar(container, orient="horizontal", command=chapter_tree.xview)
        chapter_tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)
        chapter_tree.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        # 紀錄目前每個章節的勾選狀態 {global_idx: True/False} (True = 要轉換)
        chapter_state = {}
        for i in range(1, total_chapters + 1):
            chapter_state[i] = (i not in self.excluded_chapters)

        chapter_parts = [split_chapter_title(title) for title in titles]

        # 紀錄目前表格顯示的章節編號列表
        visible_indices = []

        def _refresh_duplicates():
            nonlocal duplicate_indices
            analysis = analyze_duplicate_chapters(
                titles,
                self.catalog_data.get("chapters", []),
                use_normalized_number=duplicate_use_number.get(),
                use_chapter_name=duplicate_use_name.get(),
            )
            duplicate_indices = {int(value) for value in analysis["duplicate_indices"]}

        def _output_numbers():
            selected = [
                idx for idx in range(cur_start, cur_end + 1)
                if chapter_state.get(idx, True)
            ]
            return {source_idx: output_idx for output_idx, source_idx in enumerate(selected, 1)}

        def _update_listbox():
            chapter_tree.delete(*chapter_tree.get_children())
            visible_indices.clear()

            is_show_all = show_all_var.get()
            s_idx = 1 if is_show_all else cur_start
            e_idx = total_chapters if is_show_all else cur_end
            
            filter_text = search_entry.get().strip().lower()

            for i in range(s_idx, e_idx + 1):
                global_idx = i
                parts = chapter_parts[global_idx - 1]
                is_checked = chapter_state.get(global_idx, True)
                output_number = ""
                if is_checked and cur_start <= global_idx <= cur_end:
                    output_number = (
                        _output_numbers().get(global_idx, "")
                        if self.renumber_selected_chapters else global_idx
                    )
                values = (
                    f"{'☑' if is_checked else '☐'} {output_number}".rstrip(),
                    parts["normalized_number"],
                    parts["display_number"],
                    parts["chapter_name"],
                    "重複" if global_idx in duplicate_indices else "",
                )
                display_text = " ".join(str(value) for value in values)

                if filter_text and filter_text not in display_text.lower():
                    continue

                visible_indices.append(global_idx)
                chapter_tree.insert("", tk.END, iid=str(global_idx), values=values)

        chk_show_all = ttk.Checkbutton(control_frame, text="🌐 顯示全書章節", variable=show_all_var, command=_update_listbox)
        chk_show_all.pack(side=tk.LEFT, padx=(0, 10))

        search_entry.bind("<KeyRelease>", lambda e: _update_listbox())

        def _toggle_item(event=None):
            if event is not None and getattr(event, "num", None) == 1:
                if chapter_tree.identify_region(event.x, event.y) != "cell":
                    return
            sel = chapter_tree.selection()
            if not sel:
                return
            g_idx = int(sel[0])
            if g_idx in visible_indices:
                chapter_state[g_idx] = not chapter_state[g_idx]
                # 勾選變更可能讓後面所有製作章號前移或後移。
                _update_listbox()
                if chapter_tree.exists(str(g_idx)):
                    chapter_tree.selection_set(str(g_idx))
                    chapter_tree.focus(str(g_idx))

        # 單擊選取或按空白鍵切換狀態
        chapter_tree.bind("<ButtonRelease-1>", lambda e: _toggle_item())
        chapter_tree.bind("<space>", lambda e: (_toggle_item(), "break"))

        _refresh_duplicates()
        _update_listbox()

        # 底部按鈕區
        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X, padx=15, pady=15)

        def _select_all():
            for g_idx in visible_indices:
                chapter_state[g_idx] = True
            _update_listbox()
                
        def _deselect_all():
            for g_idx in visible_indices:
                chapter_state[g_idx] = False
            _update_listbox()

        def _invert_select():
            for g_idx in visible_indices:
                chapter_state[g_idx] = not chapter_state[g_idx]
            _update_listbox()

        def _exclude_duplicates():
            changed = 0
            for g_idx in duplicate_indices:
                if chapter_state.get(g_idx, False):
                    chapter_state[g_idx] = False
                    changed += 1
            _update_listbox()
            info_lbl.config(
                text=f"已取消勾選 {changed} 個重複章節｜目錄共標記 {len(duplicate_indices)} 個"
            )

        def _choose_duplicate_conditions():
            dialog = tk.Toplevel(top)
            dialog.title("重複判斷條件")
            dialog.resizable(False, False)
            dialog.transient(top)
            dialog.grab_set()
            body = ttk.Frame(dialog, padding=15)
            body.pack(fill=tk.BOTH, expand=True)
            pending_use_number = tk.BooleanVar(value=duplicate_use_number.get())
            pending_use_name = tk.BooleanVar(value=duplicate_use_name.get())
            ttk.Label(body, text="勾選任一符合即視為重複的條件：").pack(anchor=tk.W, pady=(0, 8))
            ttk.Checkbutton(
                body, text="網站章節數正規化", variable=pending_use_number,
            ).pack(anchor=tk.W, pady=3)
            ttk.Checkbutton(
                body, text="章節名稱（去除空白）", variable=pending_use_name,
            ).pack(anchor=tk.W, pady=3)

            def _apply_conditions():
                if not pending_use_number.get() and not pending_use_name.get():
                    messagebox.showwarning("重複判斷條件", "請至少勾選一個判斷條件。", parent=dialog)
                    return
                duplicate_use_number.set(pending_use_number.get())
                duplicate_use_name.set(pending_use_name.get())
                _refresh_duplicates()
                _update_listbox()
                info_lbl.config(text=f"已重新判斷重複章節｜共標記 {len(duplicate_indices)} 個")
                dialog.destroy()

            ttk.Button(body, text="套用", command=_apply_conditions).pack(anchor=tk.E, pady=(12, 0))

        def _renumber_selected():
            self.renumber_selected_chapters = True
            _update_listbox()
            info_lbl.config(
                text="已依勾選順序重新編號｜網站索引仍保留供下載定位"
            )

        def _close_dialog():
            self.excluded_chapters.clear()
            for g_idx, is_checked in chapter_state.items():
                if not is_checked:
                    self.excluded_chapters.add(g_idx)
            self._update_chapter_selection_summary(cur_start, cur_end)
            top.destroy()

        top.protocol("WM_DELETE_WINDOW", _close_dialog)

        ttk.Button(btn_frame, text="全選", command=_select_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="全不選", command=_deselect_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="反選", command=_invert_select).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="重複判斷條件", command=_choose_duplicate_conditions).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="排除重複章節", command=_exclude_duplicates).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="重新排序已選章節", command=_renumber_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="確定", style="Accent.TButton", command=_close_dialog).pack(side=tk.RIGHT, padx=5)

    # ── 觸發 GitHub Actions ──
    def trigger_github_actions(self):
        url = self.url_entry.get().strip()
        start_chap = self.entry_start.get().strip()
        end_chap = self.entry_end.get().strip()

        # 自動從本地 .env 或 GitHub CLI 獲取 repo 與 token
        try:
            repo, token = self._github_settings()
        except Exception as e:
            messagebox.showerror("錯誤", str(e))
            return
        
        self.current_repo = repo
        self.current_token = token
        self.current_run_id = None
        self.cancel_requested = False

        if not start_chap.isdigit() or not end_chap.isdigit():
            messagebox.showwarning("提示", "開始與結束章節必須為數字！")
            return
        if int(start_chap) < 1 or int(start_chap) > int(end_chap):
            messagebox.showwarning("提示", "章節範圍無效：開始章必須大於 0，且不能大於結束章。")
            return

        self.btn_run.config(state=tk.DISABLED)
        self.btn_cancel.config(state=tk.NORMAL)
        self.btn_download.config(state=tk.DISABLED)
        self.progress_bar.start(10)
        self.lbl_status.config(text="雲端啟動中...", foreground="#e1b12c")
        self.log(f"🚀 正向 GitHub 雲端 (Repo: {repo}) 發動並行工作流 ...")
        # 計算實際要處理的章節數（扣除已排除的）
        actual_chapters = [
            i for i in range(int(start_chap), int(end_chap) + 1)
            if i not in self.excluded_chapters
        ]
        excluded_str = ",".join(map(str, sorted(self.excluded_chapters))) if self.excluded_chapters else "無"
        self.log(f"   參數: 網址={url}, 範圍={start_chap}~{end_chap}, 實際處理 {len(actual_chapters)} 章, 排除: {excluded_str}")

        def _worker():
            try:
                # 1. 觸發 workflow_dispatch
                headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
                dispatch_url = f"https://api.github.com/repos/{repo}/actions/workflows/audiobook.yml/dispatches"
                payload = {
                    "ref": "master",
                    "inputs": {
                        "book_title": self.catalog_data.get("book_title", "待解析書名"),
                        "catalog_url": url,
                        "start_chap": start_chap,
                        "end_chap": end_chap,
                        "exclude_chapters": ",".join(map(str, sorted(list(self.excluded_chapters)))) if hasattr(self, 'excluded_chapters') and self.excluded_chapters else "",
                        "renumber_selected": "true" if self.renumber_selected_chapters else "false",
                        "zip_password": os.getenv("ZIP_PASSWORD", "Qw000000")
                    }
                }

                r = requests.post(dispatch_url, headers=headers, json=payload, timeout=15)
                if r.status_code not in (200, 204):
                    raise Exception(f"GitHub API 回應錯誤 ({r.status_code}): {r.text}")

                self.root.after(0, lambda: self.log("✓ 成功觸發 GitHub Actions 工作流！等待雲端啟動..."))

                # 2. 開始輪詢 Workflow 狀態
                time.sleep(4)
                self._poll_workflow_runs(repo, token, target_workflow_name="Audiobook Automation Pipeline (Parallel)")

            except Exception as e:
                self.root.after(0, lambda err=str(e): self._on_workflow_failed(err))

        threading.Thread(target=_worker, daemon=True).start()


    def cancel_github_actions(self):
        if not hasattr(self, 'current_run_id') or not self.current_run_id:
            messagebox.showinfo("提示", "目前沒有正在運行的任務可以取消。")
            return
            
        if hasattr(self, 'cancel_requested') and self.cancel_requested:
            return

        self.cancel_requested = True
        self.btn_cancel.config(state=tk.DISABLED)
        self.lbl_status.config(text="正在發送取消指令...", foreground="#e1b12c")
        self.log("🛑 正在向 GitHub 發送強制取消指令...")

        def _cancel_worker():
            try:
                headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.current_token}",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
                cancel_url = f"https://api.github.com/repos/{self.current_repo}/actions/runs/{self.current_run_id}/cancel"
                r = requests.post(cancel_url, headers=headers, timeout=10)
                
                if r.status_code in (202, 200, 204):
                    self.root.after(0, lambda: self.log("✓ 成功發出取消指令，等待雲端作業停止..."))
                else:
                    self.root.after(0, lambda: self.log(f"⚠ 發送取消指令失敗 (HTTP {r.status_code}): {r.text}"))
            except Exception as e:
                error_message = str(e)
                self.root.after(0, lambda message=error_message: self.log(f"⚠ 取消請求發生例外: {message}"))
                
        threading.Thread(target=_cancel_worker, daemon=True).start()

    def _poll_workflow_runs(self, repo, token, target_workflow_name=None):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        trigger_time = time.time()
        runs_url = (
            f"https://api.github.com/repos/{repo}/actions/runs"
            f"?event=workflow_dispatch&per_page=10"
        )

        connection_lost = False

        def github_get(url, **kwargs):
            """Keep monitoring alive across temporary local network outages."""
            nonlocal connection_lost
            while True:
                try:
                    response = requests.get(url, headers=headers, **kwargs)
                    if connection_lost:
                        connection_lost = False
                        self.root.after(0, lambda: self.log(
                            "🌐 網路已恢復，正在重新同步雲端 Run、Jobs 與執行紀錄…"
                        ))
                        self.root.after(0, lambda: self.lbl_status.config(
                            text="網路已恢復，正在補回狀態…", foreground="#2980b9"
                        ))
                    if response.status_code in (429, 500, 502, 503, 504):
                        try:
                            delay = int(response.headers.get("Retry-After", "5"))
                        except ValueError:
                            delay = 5
                        delay = min(60, max(5, delay))
                        if not connection_lost:
                            connection_lost = True
                            self.root.after(0, lambda c=response.status_code, d=delay: self.log(
                                f"⚠ GitHub API 暫時無法服務 (HTTP {c})，{d} 秒後重試…"
                            ))
                        time.sleep(delay)
                        continue
                    if response.status_code in (401, 403):
                        raise RuntimeError(
                            f"GitHub API 拒絕存取 (HTTP {response.status_code})；請檢查 Token 權限或 API 配額。"
                        )
                    return response
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                    if not connection_lost:
                        connection_lost = True
                        detail = str(exc)
                        self.root.after(0, lambda d=detail: self.log(
                            f"⚠️ 無法連線至 GitHub；雲端工作不受影響，GUI 將持續重試。\n   {d}"
                        ))
                        self.root.after(0, lambda: self.lbl_status.config(
                            text="網路中斷，等待重新連線…", foreground="#e1b12c"
                        ))
                    time.sleep(5)

        run_id = None
        max_wait = 40
        for attempt in range(max_wait):
            r = github_get(runs_url, timeout=10)
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                for run in runs:
                    created_at = run.get("created_at", "")
                    try:
                        from datetime import datetime
                        created_ts = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError):
                        created_ts = 0
                    # 容許 1 小時的本地時鐘誤差 (避免本地時間比伺服器快導致漏判)
                    if created_ts + 3600 < trigger_time:
                        continue
                    if target_workflow_name:
                        if run.get("name") != target_workflow_name:
                            continue
                    else:
                        if run.get("name") != "Audiobook Automation Pipeline (Parallel)":
                            continue
                    run_id = run["id"]
                    self.current_run_id = run_id
                    status = run["status"]
                    html_url = run.get("html_url", f"https://github.com/{repo}/actions/runs/{run_id}")
                    self.root.after(0, lambda rid=run_id, s=status, url=html_url, r=repo: self.log(
                        f"已連結至雲端 Run ID #{rid}，目前狀態: {s}\n"
                        f"   👉 點此查看即時進度: {url}\n"
                        f"   📂 雲端快取備份位址: https://github.com/{r}/actions/caches"
                    ))
                    break
                if run_id:
                    break
            time.sleep(2)

        if not run_id:
            raise Exception("無法取得最新的 Workflow Run！請確認權限與 Workflow 檔案狀態。")

        # 持續追蹤 Run 與 Jobs 狀態
        run_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
        jobs_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
        prev_jobs_status = {}
        seen_progress_markers = {}
        last_log_check = {}
        completed_logs_checked = set()
        upload_pause = None
        upload_complete = None
        previous_run_attempt = None

        while True:
            # 1. 查詢整體 Run 狀態
            r_run = github_get(run_url, timeout=10)
            run_status = None
            run_conclusion = None
            if r_run.status_code == 200:
                run_data = r_run.json()
                run_status = run_data.get("status")
                run_conclusion = run_data.get("conclusion")
                run_attempt = run_data.get("run_attempt", 1)
                if run_attempt != previous_run_attempt:
                    previous_run_attempt = run_attempt
                    self.root.after(0, lambda a=run_attempt: self.log(
                        f"↻ 目前雲端執行輪次：Run attempt {a}"
                    ))
                self.root.after(0, lambda s=run_status: self.lbl_status.config(text=f"雲端狀態: {s}", foreground="#2980b9"))

            # 2. 查詢並行 Jobs 狀態 (支援分頁，避免超過 30 個 Job 就印不出來)
            all_jobs = []
            page = 1
            while True:
                paged_url = f"{jobs_url}?per_page=100&page={page}"
                r_jobs = github_get(paged_url, timeout=10)
                if r_jobs.status_code == 200:
                    jobs = r_jobs.json().get("jobs", [])
                    if not jobs:
                        break
                    all_jobs.extend(jobs)
                    if len(jobs) < 100:
                        break
                    page += 1
                else:
                    break

            for job in all_jobs:
                j_id = job.get("id")
                j_name = job.get("name")
                j_status = job.get("status")
                j_conc = job.get("conclusion")
                key = f"{j_status}_{j_conc}"

                if prev_jobs_status.get(j_name) != key:
                    prev_jobs_status[j_name] = key
                    msg = f" └─ Job [{j_name}]: {j_status}"
                    if j_conc:
                        msg += f" ({j_conc})"
                    self.root.after(0, lambda m=msg: self.log(m))

                # 3. 抓取執行中與剛完成的 Job Log；復網後可補回離線期間的章節進度
                should_check_log = (
                    j_status == "in_progress"
                    or (j_status == "completed" and j_id not in completed_logs_checked)
                )
                if should_check_log and j_id:
                    now = time.time()
                    if j_status == "completed" or now - last_log_check.get(j_id, 0) >= 12:
                        last_log_check[j_id] = now
                        try:
                            log_url = f"https://api.github.com/repos/{repo}/actions/jobs/{j_id}/logs"
                            r_log = github_get(log_url, timeout=5, allow_redirects=True)
                            if r_log.status_code == 200:
                                if j_status == "completed":
                                    completed_logs_checked.add(j_id)
                                if j_id not in seen_progress_markers:
                                    seen_progress_markers[j_id] = set()

                                # 解析標籤 [PROGRESS_MARKER]
                                matches = re.findall(
                                    r'\[PROGRESS_MARKER\] Worker-(\d+) \| Ch (\S+) (?:complete|done) \((\d+/\d+)\)',
                                    r_log.text
                                )
                                for w_id, ch_range, prog in matches:
                                    marker_key = f"{w_id}_{ch_range}_{prog}"
                                    if marker_key not in seen_progress_markers[j_id]:
                                        seen_progress_markers[j_id].add(marker_key)
                                        p_msg = f"     ├─ ⚡ [Worker {w_id}] ✅ 第 {ch_range} 章一條龍合成完成 (進度: {prog})"
                                        self.root.after(0, lambda m=p_msg: self.log(m))

                                # 解析 API 上傳標籤 [API_UPLOAD_MARKER]
                                api_matches = re.findall(
                                    r'\[API_UPLOAD_MARKER\] (START|DONE) \| Part (\S+) \| Ch (\S+) \| (.+)',
                                    r_log.text
                                )
                                for action, item_prog, chap_str, detail in api_matches:
                                    a_key = f"api_upload_{action}_{item_prog}_{chap_str}"
                                    if a_key not in seen_progress_markers[j_id]:
                                        seen_progress_markers[j_id].add(a_key)
                                        if action == "START":
                                            p_msg = f"     ├─ 📤 [API上傳進度] [{item_prog}] ▶️ 開始極速上傳: {chap_str} ({detail})"
                                        else:
                                            p_msg = f"     ├─ 📤 [API上傳進度] [{item_prog}] ✅ 成功上傳並加入播放清單: {chap_str} ({detail})"
                                        self.root.after(0, lambda m=p_msg: self.log(m))

                                status_matches = re.findall(
                                    r'\[API_UPLOAD_STATUS\] (PAUSED|COMPLETE) \| uploaded=(\d+) \| total=(\d+)'
                                    r'(?: \| retry_at=([^ |]+) \| source_run=([^ |]+) \| reason=([^\r\n]+)'
                                    r'| \| source_run=([^ |]+))',
                                    r_log.text,
                                )
                                for state, uploaded, total, retry_at, source_run, reason, complete_source in status_matches:
                                    if state == "PAUSED":
                                        upload_pause = {
                                            "uploaded": int(uploaded), "total": int(total),
                                            "retry_at": retry_at, "source_run": source_run, "reason": reason.strip(),
                                        }
                                    else:
                                        upload_complete = {"uploaded": int(uploaded), "total": int(total)}

                                for summary_text in re.findall(r'\[RUN_SUMMARY\] (\{[^\r\n]+\})', r_log.text):
                                    summary_key = "summary_" + summary_text
                                    if summary_key in seen_progress_markers[j_id]:
                                        continue
                                    seen_progress_markers[j_id].add(summary_key)
                                    try:
                                        summary = json.loads(summary_text)
                                        if summary.get("kind") == "worker":
                                            incomplete = summary.get("incomplete") or []
                                            detail = f"；未完成章節：{incomplete}" if incomplete else ""
                                            message = (
                                                f"     ├─ 📋 [Worker {summary['worker']} 摘要] "
                                                f"完成 {summary['completed']}/{summary['total']} 章{detail}"
                                            )
                                        else:
                                            message = (
                                                f"     ├─ 📋 [YouTube 摘要] {summary.get('status')}，"
                                                f"完成 {summary.get('completed')}/{summary.get('total')} Parts，"
                                                f"待補封面 {summary.get('pending_thumbnails')}、CC {summary.get('pending_captions')}、"
                                                f"播放清單 {summary.get('pending_playlist')}、發布 {summary.get('pending_publish')}"
                                            )
                                        self.root.after(0, lambda m=message: self.log(m))
                                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                                        pass

                                # 相容備用：解析 "批次完成：第 X~Y 章"
                                fallback_matches = re.findall(
                                    r'=== \[Worker-(\d+)\] ✅ 批次完成：第 (\S+) 章 MP4 影片已實打實寫入 Workspace/！ ===',
                                    r_log.text
                                )
                                for w_id, ch_range in fallback_matches:
                                    marker_key = f"fb_{w_id}_{ch_range}"
                                    if marker_key not in seen_progress_markers[j_id]:
                                        seen_progress_markers[j_id].add(marker_key)
                                        p_msg = f"     ├─ ⚡ [Worker {w_id}] ✅ 第 {ch_range} 章寫入雲端"
                                        self.root.after(0, lambda m=p_msg: self.log(m))
                        except Exception:
                            pass

            # Jobs and logs are synchronized before showing the terminal result, so
            # progress produced while the GUI was offline is not skipped.
            if run_status == "completed":
                if upload_pause:
                    retry_text = upload_pause["retry_at"]
                    self.root.after(0, lambda p=upload_pause, t=retry_text: self._on_workflow_failed(
                        f"YouTube 上傳尚未完成：{p['uploaded']}/{p['total']} 部。"
                        f" 原因：{p['reason']}。安全重試時間：{t}。"
                        " 系統會每 15 分鐘檢查；到達安全重試時間後自動斷點續傳。"
                    ))
                elif run_conclusion == "success" and upload_complete:
                    self.root.after(0, lambda: self._on_workflow_success(repo, run_id))
                elif run_conclusion == "success" and target_workflow_name == "Fast Upload Audiobooks & Build YouTube Playlist":
                    self.root.after(0, lambda: self._on_workflow_failed(
                        "GitHub Job 結束，但未找到 YouTube COMPLETE 標記；不得判定為全部上傳完成。"
                    ))
                elif run_conclusion == "success":
                    self.root.after(0, lambda: self._on_workflow_success(repo, run_id))
                else:
                    self.root.after(0, lambda c=run_conclusion: self._on_workflow_failed(f"雲端執行失敗: {c}"))
                break

            time.sleep(5)

    def _show_success_dialog(self, title, msg, url):
        top = tk.Toplevel(self.root)
        top.title(title)
        top.geometry("500x200")
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()

        frame = ttk.Frame(top, padding="20")
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=msg, font=("Microsoft JhengHei", 10)).pack(pady=(0, 10))

        url_lbl = ttk.Label(frame, text=url, foreground="blue", cursor="hand2", font=("Microsoft JhengHei", 10, "underline"))
        url_lbl.pack(pady=(0, 15))
        url_lbl.bind("<Button-1>", lambda e: webbrowser.open(url))

        ttk.Button(frame, text="確定", command=top.destroy).pack()

    def _on_workflow_success(self, repo, run_id):
        self.progress_bar.stop()
        self.btn_run.config(state=tk.NORMAL)
        self.btn_download.config(state=tk.NORMAL)
        self.lbl_status.config(text="✅ 已確認 YouTube 上傳全部成功！", foreground="#27ae60")
        
        # 異步獲取 Actions Artifacts 大小資訊並印出到 Log
        def _fetch_artifact_info():
            try:
                headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.current_token}",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
                api_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
                r = requests.get(api_url, headers=headers, timeout=10)
                total_bytes = 0
                asset_info_list = []
                
                if r.status_code == 200:
                    assets = [item for item in r.json().get("artifacts", []) if not item.get("expired")]
                    for asset in assets:
                        import urllib.parse
                        raw_name = asset["name"]
                        name = urllib.parse.unquote(raw_name)
                        if name.startswith("default.") and hasattr(self, 'catalog_data') and self.catalog_data and self.catalog_data.get("book_title"):
                            name = name.replace("default", self.catalog_data["book_title"], 1)
                        
                        sz_bytes = asset.get("size_in_bytes", 0)
                        total_bytes += sz_bytes
                        sz_mb = sz_bytes / (1024 * 1024)
                        if sz_mb >= 1024:
                            sz_str = f"{sz_mb / 1024:.2f} GB"
                        else:
                            sz_str = f"{sz_mb:.1f} MB"
                        asset_info_list.append(f"   └─ 📄 {name} ({sz_str})")
                
                total_mb = total_bytes / (1024 * 1024)
                if total_mb >= 1024:
                    total_str = f"{total_mb / 1024:.2f} GB"
                else:
                    total_str = f"{total_mb:.1f} MB"
                
                def _update_log():
                    self.log("==========================================")
                    self.log("✅ 已確認所有影片、字幕與播放清單均成功發布到 YouTube。")
                    self.log("🛑 完整成功檢查已通過，不需要再啟動重試。")
                    if asset_info_list:
                        self.log(f"📦 雲端產物總大小：【{total_str}】(共 {len(asset_info_list)} 個檔案)")
                        for item in asset_info_list:
                            self.log(item)
                    self.log("💡 若您需要下載所有檔案到本地，請點擊上方的【📥 一鍵下載成品】按鈕。")
                    self.log("==========================================")
                
                self.root.after(0, _update_log)
            except Exception as e:
                error_message = str(e)
                self.root.after(0, lambda message=error_message: self.log(
                    f"✅ 已確認 YouTube 上傳全部成功！（無法讀取檔案大小: {message}）"
                ))
                
        threading.Thread(target=_fetch_artifact_info, daemon=True).start()

    def start_batch_download(self):
        if not hasattr(self, 'current_run_id') or not self.current_run_id:
            messagebox.showwarning("提示", "沒有可下載的任務紀錄！")
            return
            
        self.btn_download.config(state=tk.DISABLED)
        self.lbl_status.config(text="準備下載中...", foreground="#2980b9")
        self.log("📥 開始批量下載所有分割檔案，請勿關閉視窗...")
        repo = self.current_repo
        run_id = self.current_run_id
        
        def _download_worker():
            try:
                headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.current_token}",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
                api_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
                r = requests.get(api_url, headers=headers, timeout=10)
                if r.status_code != 200:
                    raise Exception(f"無法取得 Actions Artifacts: {r.text}")
                
                assets = [item for item in r.json().get("artifacts", []) if not item.get("expired")]
                if not assets:
                    raise Exception("此 Run 找不到尚未過期的 Actions Artifact！")
                
                os.makedirs("Downloads", exist_ok=True)
                total = len(assets)
                
                for idx, asset in enumerate(assets, 1):
                    import urllib.parse
                    raw_name = asset["name"] + ".zip"
                    name = urllib.parse.unquote(raw_name)
                    
                    # 處理舊版本 GitHub 把 % 替換成 . 的編碼檔名 (例如 E5.87.A1.E4.BA.BA...zip)
                    dot_hex_pattern = r'^([A-Fa-f0-9]{2}\.)+[A-Fa-f0-9]{2}(\.zip|\.z\d+)$'
                    if re.match(dot_hex_pattern, name):
                        base_part, ext_part = os.path.splitext(name)
                        try:
                            percent_str = "%" + "%".join(base_part.split('.'))
                            decoded = urllib.parse.unquote(percent_str)
                            if decoded and not decoded.startswith("%"):
                                name = decoded + ext_part
                        except Exception:
                            pass

                    if name.startswith("default.") and hasattr(self, 'catalog_data') and self.catalog_data and self.catalog_data.get("book_title"):
                        name = name.replace("default", self.catalog_data["book_title"], 1)
                        
                    size_bytes = asset.get("size_in_bytes", 0)
                    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                    
                    self.root.after(0, lambda n=name, i=idx, t=total, s=size_mb: self.log(f"📥 正在下載 ({i}/{t}): {n} (大小: {s:.1f} MB) ..."))
                    
                    asset_api_url = asset["archive_download_url"]
                    headers_dl = {"Authorization": f"Bearer {self.current_token}", "Accept": "application/vnd.github+json"}
                    r_dl = requests.get(asset_api_url, headers=headers_dl, stream=True)
                    
                    if r_dl.status_code in (200, 302):
                        file_path = os.path.join("Downloads", name)
                        downloaded = 0
                        chunk_size = 1024 * 1024  # 1MB 緩衝，極速下載
                        last_update_time = time.time()
                        
                        with open(file_path, "wb") as f:
                            for chunk in r_dl.iter_content(chunk_size=chunk_size):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    now = time.time()
                                    # 每秒或下載完畢時更新進度
                                    if now - last_update_time >= 1.5 or (size_bytes and downloaded >= size_bytes):
                                        last_update_time = now
                                        dl_mb = downloaded / (1024 * 1024)
                                        pct = (downloaded / size_bytes * 100) if size_bytes else 0
                                        status_str = f"下載中: {name} ({pct:.0f}%)" if size_bytes else f"下載中: {name} ({dl_mb:.1f} MB)"
                                        log_str = f"   └─ 進度: {dl_mb:.1f} MB / {size_mb:.1f} MB ({pct:.1f}%)" if size_bytes else f"   └─ 進度: {dl_mb:.1f} MB"
                                        self.root.after(0, lambda l=log_str, s=status_str: (
                                            self.log(l),
                                            self.lbl_status.config(text=s, foreground="#2980b9")
                                        ))
                    else:
                        raise Exception(f"下載 {name} 失敗: HTTP {r_dl.status_code}")
                
                self.root.after(0, lambda: self.log("✅ 所有檔案下載完畢！請至 Downloads 資料夾解壓縮。"))
                self.root.after(0, lambda: self.lbl_status.config(text="下載完成", foreground="#27ae60"))
                self.root.after(0, lambda: self.btn_download.config(state=tk.NORMAL))
                self.root.after(0, lambda: messagebox.showinfo("下載完成", f"共 {total} 個檔案已儲存至 Downloads 資料夾！\n\n請對第一個 .zip 點擊右鍵解壓縮即可！"))
                
            except Exception as e:
                self.root.after(0, lambda err=e: self.log(f"⚠ 下載失敗: {err}"))
                self.root.after(0, lambda: self.lbl_status.config(text="下載失敗", foreground="#e74c3c"))
                self.root.after(0, lambda: self.btn_download.config(state=tk.NORMAL))

        threading.Thread(target=_download_worker, daemon=True).start()

    def _on_workflow_failed(self, err_msg):
        self.progress_bar.stop()
        self.btn_run.config(state=tk.NORMAL)
        self.lbl_status.config(text="執行失敗", foreground="#e74c3c")
        self.log(f"✗ {err_msg}")
        messagebox.showerror("錯誤", f"發動雲端執行發生錯誤：\n{err_msg}")

if __name__ == "__main__":
    root = tk.Tk()
    app = AudiobookGUIApp(root)
    root.mainloop()
