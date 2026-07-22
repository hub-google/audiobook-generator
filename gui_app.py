import os
import sys
import json
import time
import requests
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog
from dotenv import load_dotenv
import re
import webbrowser

# 載入目錄解析器
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
try:
    from catalog_parser import parse_catalog
except ImportError:
    parse_catalog = None

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

class AudiobookGUIApp:
    def __init__(self, root):
        self.root = root
        self.root.title("📚 GITHUB ACTION控制台")
        self.root.geometry("780x620")
        self.root.minsize(700, 550)

        # 狀態變數
        self.catalog_data = None

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

        # 標題列
        title_lbl = ttk.Label(main_frame, text="📚 GITHUB ACTION控制台", style="Title.TLabel")
        title_lbl.pack(anchor=tk.W, pady=(0, 15))

        # ── 區塊 1: 目錄網址與章節解析 ──
        section1 = ttk.LabelFrame(main_frame, text="1. 目錄解析與範圍選取")
        section1.pack(fill=tk.X, pady=(0, 15))

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
        
        self.excluded_chapters = set()


        # ── 區塊 2: 雲端製作控管 ──
        section2 = ttk.LabelFrame(main_frame, text="2. 雲端製作控管")
        section2.pack(fill=tk.BOTH, expand=True)

        # Google Drive inputs removed
        action_frame = ttk.Frame(section2)
        action_frame.pack(fill=tk.X, pady=5)

        self.btn_run = ttk.Button(action_frame, text="🚀 發動 GitHub Actions 雲端製作", style="Accent.TButton", command=self.trigger_github_actions)
        self.btn_run.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_api_upload = ttk.Button(action_frame, text="📤 暴速上傳 YouTube (建播放清單)", command=self.trigger_youtube_api_upload)
        self.btn_api_upload.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_cancel = ttk.Button(action_frame, text="🛑 取消雲端作業", command=self.cancel_github_actions, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=(0, 15))

        self.btn_download = ttk.Button(action_frame, text="📥 一鍵下載成品", command=self.start_batch_download, state=tk.DISABLED)
        self.btn_download.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_status = ttk.Label(action_frame, text="就緒", style="Status.TLabel")
        self.lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 進度條
        self.progress_bar = ttk.Progressbar(section2, mode="indeterminate")
        self.progress_bar.pack(fill=tk.X, pady=5)

        # 實時 Log 控制台
        ttk.Label(section2, text="雲端執行日誌 (Cloud Logs):").pack(anchor=tk.W, pady=(5, 2))
        self.log_text = scrolledtext.ScrolledText(section2, height=12, background="#1e1e1e", foreground="#dcdcdc", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # 設定超連結標籤
        self.log_text.tag_config("hyperlink", foreground="#4da6ff", underline=1)
        self.log_text.tag_bind("hyperlink", "<Enter>", lambda e: self.log_text.config(cursor="hand2"))
        self.log_text.tag_bind("hyperlink", "<Leave>", lambda e: self.log_text.config(cursor=""))
        self.link_counter = 0

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
                self.root.after(0, lambda: self._on_parse_failed(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_parse_success(self, res):
        self.btn_parse.config(state=tk.NORMAL)
        if res and res.get("success"):
            self.catalog_data = res
            book_title = res["book_title"]
            total = res["total_chapters"]

            self.lbl_book_info.config(text=f"書名: {book_title} | 總章節: {total} 章", foreground="#27ae60")
            self.entry_start.delete(0, tk.END)
            self.entry_start.insert(0, "1")
            self.entry_end.delete(0, tk.END)
            self.entry_end.insert(0, str(total))
            
            self.btn_filter.config(state=tk.NORMAL)
            self.excluded_chapters.clear()

            self.lbl_status.config(text="解析完成", foreground="#27ae60")
            self.log(f"✓ 解析成功！書名:【{book_title}】，共找到 {total} 章節。")
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
            
        try:
            start_idx = int(self.entry_start.get().strip())
            end_idx = int(self.entry_end.get().strip())
        except ValueError:
            messagebox.showwarning("提示", "開始與結束章節必須為數字！")
            return
            
        titles = self.catalog_data.get("chapter_titles", [])
        if not titles:
            messagebox.showinfo("提示", "目前沒有章節標題資訊可供篩選。")
            return
            
        start_idx = max(1, start_idx)
        end_idx = min(len(titles), end_idx)
        
        top = tk.Toplevel(self.root)
        top.title("選擇要轉換的章節")
        top.geometry("400x500")
        top.transient(self.root)
        top.grab_set()

        ttk.Label(top, text="請取消勾選「不想轉換」的章節 (例如：請假單)", font=("Microsoft JhengHei", 10, "bold")).pack(pady=10)

        # 加上全選/全不選按鈕
        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X, padx=10)

        # 中間捲動區塊
        canvas = tk.Canvas(top, highlightthickness=0)
        scrollbar = ttk.Scrollbar(top, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=10, pady=5)
        scrollbar.pack(side="right", fill="y")
        
        # 滑鼠滾輪支援
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self.checkbox_vars = {}
        for i in range(start_idx, end_idx + 1):
            global_idx = i
            title = titles[i - 1]
            var = tk.BooleanVar(value=(global_idx not in self.excluded_chapters))
            self.checkbox_vars[global_idx] = var
            cb = ttk.Checkbutton(scrollable_frame, text=f"第 {global_idx} 章: {title}", variable=var)
            cb.pack(anchor="w", pady=2)
            
        def _select_all():
            for var in self.checkbox_vars.values():
                var.set(True)
                
        def _deselect_all():
            for var in self.checkbox_vars.values():
                var.set(False)

        def _save():
            self.excluded_chapters.clear()
            for g_idx, var in self.checkbox_vars.items():
                if not var.get():
                    self.excluded_chapters.add(g_idx)
            top.destroy()
            canvas.unbind_all("<MouseWheel>")

        ttk.Button(btn_frame, text="全選", command=_select_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="全不選", command=_deselect_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="確定", style="Accent.TButton", command=_save).pack(side=tk.RIGHT, padx=5)

    # ── 觸發 GitHub Actions ──
    def trigger_github_actions(self):
        url = self.url_entry.get().strip()
        start_chap = self.entry_start.get().strip()
        end_chap = self.entry_end.get().strip()

        # 自動從本地 .env 獲取 repo 與 token
        load_dotenv(ENV_PATH, override=True)
        repo = os.getenv("GITHUB_REPO", "hub-google/audiobook-generator")
        token = os.getenv("GITHUB_TOKEN", "")
        
        self.current_repo = repo
        self.current_token = token
        self.current_run_id = None
        self.cancel_requested = False

        if not token:
            messagebox.showerror("錯誤", "本地 .env 中未找到 GITHUB_TOKEN！請確認檔案。")
            return

        if not start_chap.isdigit() or not end_chap.isdigit():
            messagebox.showwarning("提示", "開始與結束章節必須為數字！")
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
                        "catalog_url": url,
                        "start_chap": start_chap,
                        "end_chap": end_chap,
                        "exclude_chapters": ",".join(map(str, sorted(list(self.excluded_chapters)))) if hasattr(self, 'excluded_chapters') and self.excluded_chapters else "",
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


    def trigger_youtube_api_upload(self):
        load_dotenv(ENV_PATH, override=True)
        repo = os.getenv("GITHUB_REPO", "hub-google/audiobook-generator")
        token = os.getenv("GITHUB_TOKEN", "")

        if not token:
            messagebox.showerror("錯誤", "本地 .env 中未找到 GITHUB_TOKEN！請確認檔案。")
            return

        default_run_id = getattr(self, "current_run_id", "") or ""
        run_id = simpledialog.askstring(
            "暴速上傳 YouTube (自動建播放清單)",
            "請輸入包含影片 Artifacts 的 GitHub Run ID:\n(如不確定可維持預設值或至 GitHub 複製)",
            initialvalue=str(default_run_id) if default_run_id else "29821206020"
        )
        if not run_id or not run_id.strip():
            return

        run_id = run_id.strip()
        self.current_repo = repo
        self.current_token = token
        self.current_run_id = None
        self.cancel_requested = False

        self.btn_run.config(state=tk.DISABLED)
        if hasattr(self, 'btn_api_upload'):
            self.btn_api_upload.config(state=tk.DISABLED)
        if hasattr(self, 'btn_stream'):
            self.btn_stream.config(state=tk.DISABLED)
        self.btn_cancel.config(state=tk.NORMAL)
        self.progress_bar.start(10)
        self.lbl_status.config(text="啟動暴速 API 上傳中...", foreground="#e1b12c")
        self.log(f"📤 正向 GitHub (Repo: {repo}) 發動 YouTube API 極速上傳與播放清單建置 (Target Run ID: {run_id}) ...")

        def _worker():
            try:
                headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
                dispatch_url = f"https://api.github.com/repos/{repo}/actions/workflows/youtube_upload.yml/dispatches"
                payload = {
                    "ref": "master",
                    "inputs": {
                        "run_id": run_id,
                        "privacy": "public"
                    }
                }

                r = requests.post(dispatch_url, headers=headers, json=payload, timeout=15)
                if r.status_code not in (200, 204):
                    raise Exception(f"GitHub API 回應錯誤 ({r.status_code}): {r.text}")

                self.root.after(0, lambda: self.log("✓ 成功發動 YouTube API 暴速上傳 Workflow！等待雲端啟動..."))
                time.sleep(4)
                self._poll_workflow_runs(repo, token, target_workflow_name="Fast Upload Audiobooks & Build YouTube Playlist")

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
                self.root.after(0, lambda: self.log(f"⚠ 取消請求發生例外: {e}"))
                
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

        run_id = None
        max_wait = 40
        for attempt in range(max_wait):
            r = requests.get(runs_url, headers=headers, timeout=10)
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                for run in runs:
                    if run.get("status") == "completed":
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
                    self.root.after(0, lambda s=status, url=html_url, r=repo: self.log(
                        f"已連結至雲端 Run ID #{run_id}，目前狀態: {s}\n"
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

        while True:
            # 1. 查詢整體 Run 狀態
            r_run = requests.get(run_url, headers=headers, timeout=10)
            if r_run.status_code == 200:
                run_data = r_run.json()
                run_status = run_data.get("status")
                run_conclusion = run_data.get("conclusion")
                self.root.after(0, lambda s=run_status: self.lbl_status.config(text=f"雲端狀態: {s}", foreground="#2980b9"))

                if run_status == "completed":
                    if run_conclusion == "success":
                        self.root.after(0, lambda: self._on_workflow_success(repo, run_id))
                    else:
                        self.root.after(0, lambda c=run_conclusion: self._on_workflow_failed(f"雲端執行失敗: {c}"))
                    break

            # 2. 查詢並行 Jobs 狀態 (支援分頁，避免超過 30 個 Job 就印不出來)
            all_jobs = []
            page = 1
            while True:
                paged_url = f"{jobs_url}?per_page=100&page={page}"
                r_jobs = requests.get(paged_url, headers=headers, timeout=10)
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

                # 3. 針對正在執行的 Worker Job，抓取即時 Log 解析章節進度
                if j_status == "in_progress" and j_id:
                    now = time.time()
                    if now - last_log_check.get(j_id, 0) >= 12:
                        last_log_check[j_id] = now
                        try:
                            log_url = f"https://api.github.com/repos/{repo}/actions/jobs/{j_id}/logs"
                            r_log = requests.get(log_url, headers=headers, timeout=5, allow_redirects=True)
                            if r_log.status_code == 200:
                                if j_id not in seen_progress_markers:
                                    seen_progress_markers[j_id] = set()

                                # 解析標籤 [PROGRESS_MARKER]
                                matches = re.findall(
                                    r'\[PROGRESS_MARKER\] Worker-(\d+) \| Ch (\S+) done \((\d+/\d+)\)',
                                    r_log.text
                                )
                                for w_id, ch_range, prog in matches:
                                    marker_key = f"{w_id}_{ch_range}_{prog}"
                                    if marker_key not in seen_progress_markers[j_id]:
                                        seen_progress_markers[j_id].add(marker_key)
                                        p_msg = f"     ├─ ⚡ [Worker {w_id}] ✅ 第 {ch_range} 章一條龍合成完成 (進度: {prog})"
                                        self.root.after(0, lambda m=p_msg: self.log(m))

                                # 解析直播標籤 [STREAM_MARKER]
                                stream_matches = re.findall(
                                    r'\[STREAM_MARKER\] (START|DONE) \| (\S+) \| Ch (\S+) \| (\S+)(?: \| total (\d+))?',
                                    r_log.text
                                )
                                for action, w_info, ch_prog, chap_name, total_cnt in stream_matches:
                                    s_key = f"stream_{action}_{w_info}_{chap_name}"
                                    if s_key not in seen_progress_markers[j_id]:
                                        seen_progress_markers[j_id].add(s_key)
                                        if action == "START":
                                            p_msg = f"     ├─ 🎥 [直播進度] [{w_info}] ▶️ 開始推流: {chap_name} (章節進度: {ch_prog})"
                                        else:
                                            p_msg = f"     ├─ 🎥 [直播進度] [{w_info}] ✅ 完成推流: {chap_name} (累計已推流: {total_cnt or '?'} 章)"
                                        self.root.after(0, lambda m=p_msg: self.log(m))

                                # 解析 API 上傳標籤 [API_UPLOAD_MARKER]
                                api_matches = re.findall(
                                    r'\[API_UPLOAD_MARKER\] (START|DONE) \| Item (\S+) \| (\S+) \| (.+)',
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
        if hasattr(self, 'btn_api_upload'):
            self.btn_api_upload.config(state=tk.NORMAL)
        if hasattr(self, 'btn_stream'):
            self.btn_stream.config(state=tk.NORMAL)
        self.btn_download.config(state=tk.NORMAL)
        self.lbl_status.config(text="🎉 雲端作業完成！", foreground="#27ae60")
        
        # 異步獲取 Release 檔案大小資訊並印出到 Log
        def _fetch_release_info():
            try:
                headers = {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.current_token}",
                    "X-GitHub-Api-Version": "2022-11-28"
                }
                api_url = f"https://api.github.com/repos/{repo}/releases/tags/run-{run_id}"
                r = requests.get(api_url, headers=headers, timeout=10)
                total_bytes = 0
                asset_info_list = []
                
                if r.status_code == 200:
                    assets = r.json().get("assets", [])
                    for asset in assets:
                        import urllib.parse
                        raw_name = asset["name"]
                        name = urllib.parse.unquote(raw_name)
                        if name.startswith("default.") and hasattr(self, 'catalog_data') and self.catalog_data and self.catalog_data.get("book_title"):
                            name = name.replace("default", self.catalog_data["book_title"], 1)
                        
                        sz_bytes = asset.get("size", 0)
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
                    self.log("🎉 恭喜！GitHub Actions 雲端工作流成功執行完畢。")
                    if asset_info_list:
                        self.log(f"📦 雲端產物總大小：【{total_str}】(共 {len(asset_info_list)} 個檔案)")
                        for item in asset_info_list:
                            self.log(item)
                    self.log("💡 若您需要下載所有檔案到本地，請點擊上方的【📥 一鍵下載成品】按鈕。")
                    self.log("==========================================")
                
                self.root.after(0, _update_log)
            except Exception as e:
                self.root.after(0, lambda: self.log(f"🎉 雲端工作流成功執行完畢！（無法讀取檔案大小: {e}）"))
                
        threading.Thread(target=_fetch_release_info, daemon=True).start()

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
                api_url = f"https://api.github.com/repos/{repo}/releases/tags/run-{run_id}"
                r = requests.get(api_url, headers=headers, timeout=10)
                if r.status_code != 200:
                    raise Exception(f"無法取得 Release 資訊: {r.text}")
                
                assets = r.json().get("assets", [])
                if not assets:
                    raise Exception("Release 中找不到任何檔案！")
                
                os.makedirs("Downloads", exist_ok=True)
                total = len(assets)
                
                for idx, asset in enumerate(assets, 1):
                    import urllib.parse
                    raw_name = asset["name"]
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
                        
                    size_bytes = asset.get("size", 0)
                    size_mb = size_bytes / (1024 * 1024) if size_bytes else 0
                    
                    self.root.after(0, lambda n=name, i=idx, t=total, s=size_mb: self.log(f"📥 正在下載 ({i}/{t}): {n} (大小: {s:.1f} MB) ..."))
                    
                    asset_api_url = asset["url"]
                    headers_dl = {"Authorization": f"token {self.current_token}", "Accept": "application/octet-stream"}
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
        if hasattr(self, 'btn_api_upload'):
            self.btn_api_upload.config(state=tk.NORMAL)
        if hasattr(self, 'btn_stream'):
            self.btn_stream.config(state=tk.NORMAL)
        self.lbl_status.config(text="執行失敗", foreground="#e74c3c")
        self.log(f"✗ {err_msg}")
        messagebox.showerror("錯誤", f"發動雲端執行發生錯誤：\n{err_msg}")

if __name__ == "__main__":
    root = tk.Tk()
    app = AudiobookGUIApp(root)
    root.mainloop()
