import os
import sys
import json
import time
import requests
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from dotenv import load_dotenv, set_key

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
        self.root.title("📚 AI 有聲小說雲端製作控制台")
        self.root.geometry("780x720")
        self.root.minsize(700, 650)

        # 狀態變數
        self.catalog_data = None
        self.polling_active = False

        self._setup_style()
        self._build_ui()
        self._load_saved_config()

    def _setup_style(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        # 顏色配與主題
        BG_COLOR = "#f5f6fa"
        PRIMARY_COLOR = "#2f3640"
        ACCENT_COLOR = "#0097e6"
        
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
        title_lbl = ttk.Label(main_frame, text="📚 有聲小說雲端自動化製作控制台", style="Title.TLabel")
        title_lbl.pack(anchor=tk.W, pady=(0, 15))

        # ── 區塊 1: 小說目錄網址與章節解析 ──
        section1 = ttk.LabelFrame(main_frame, text="1. 小說目錄解析與範圍選取")
        section1.pack(fill=tk.X, pady=(0, 10))

        url_frame = ttk.Frame(section1)
        url_frame.pack(fill=tk.X, pady=5)

        ttk.Label(url_frame, text="小說目錄網址:").pack(side=tk.LEFT, padx=(0, 5))
        self.url_entry = ttk.Entry(url_frame, width=50)
        self.url_entry.insert(0, "https://tw.hjwzw.com/Book/1644")
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
        self.entry_end.pack(side=tk.LEFT)

        # ── 區塊 2: GitHub 憑證設定 ──
        section2 = ttk.LabelFrame(main_frame, text="2. GitHub 隱私設定 (用於觸發 GitHub Actions)")
        section2.pack(fill=tk.X, pady=(0, 10))

        gh_frame = ttk.Frame(section2)
        gh_frame.pack(fill=tk.X, pady=5)

        ttk.Label(gh_frame, text="Repository (owner/repo):").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.entry_repo = ttk.Entry(gh_frame, width=30)
        self.entry_repo.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(gh_frame, text="GitHub Access Token (PAT):").grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        self.entry_token = ttk.Entry(gh_frame, width=40, show="*")
        self.entry_token.grid(row=1, column=1, sticky=tk.W, padx=5, pady=2)

        btn_save_config = ttk.Button(gh_frame, text="💾 儲存憑證", command=self.save_github_credentials)
        btn_save_config.grid(row=1, column=2, padx=10, pady=2)

        # ── 區塊 3: 控制按鈕與即時進度 ──
        section3 = ttk.LabelFrame(main_frame, text="3. 雲端製作控管")
        section3.pack(fill=tk.BOTH, expand=True)

        action_frame = ttk.Frame(section3)
        action_frame.pack(fill=tk.X, pady=5)

        self.btn_run = ttk.Button(action_frame, text="🚀 發動 GitHub Actions 雲端製作", style="Accent.TButton", command=self.trigger_github_actions)
        self.btn_run.pack(side=tk.LEFT, padx=(0, 15))

        self.lbl_status = ttk.Label(action_frame, text="就緒", style="Status.TLabel")
        self.lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 進度條
        self.progress_bar = ttk.Progressbar(section3, mode="indeterminate")
        self.progress_bar.pack(fill=tk.X, pady=5)

        # 實時 Log 控制台
        ttk.Label(section3, text="雲端執行日誌 (Cloud Logs):").pack(anchor=tk.W, pady=(5, 2))
        self.log_text = scrolledtext.ScrolledText(section3, height=12, background="#1e1e1e", foreground="#dcdcdc", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}\n"
        self.log_text.insert(tk.END, formatted_msg)
        self.log_text.see(tk.END)

    def _load_saved_config(self):
        repo = os.getenv("GITHUB_REPO", "")
        token = os.getenv("GITHUB_TOKEN", "")
        if repo:
            self.entry_repo.insert(0, repo)
        if token:
            self.entry_token.insert(0, token)

    def save_github_credentials(self):
        repo = self.entry_repo.get().strip()
        token = self.entry_token.get().strip()
        if not repo or not token:
            messagebox.showwarning("提示", "請輸入完整的 Repo 名稱與 GitHub Token！")
            return

        set_key(ENV_PATH, "GITHUB_REPO", repo)
        set_key(ENV_PATH, "GITHUB_TOKEN", token)
        messagebox.showinfo("成功", "GitHub 憑證已安全儲存於本地 .env！")
        self.log("✓ 已儲存 GitHub 憑證至本地 .env (已獲 .gitignore 保護)。")

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
            self.entry_end.insert(0, str(min(10, total)))

            self.lbl_status.config(text="解析完成", foreground="#27ae60")
            self.log(f"✓ 解析成功！書名:【{book_title}】，共找到 {total} 章節。")
        else:
            self._on_parse_failed("無法找到章節內容")

    def _on_parse_failed(self, error_msg):
        self.btn_parse.config(state=tk.NORMAL)
        self.lbl_status.config(text="解析失敗", foreground="#e74c3c")
        self.log(f"✗ 目錄解析失敗: {error_msg}")
        messagebox.showerror("解析失敗", f"無法讀取該目錄網址：\n{error_msg}")

    # ── 觸發 GitHub Actions ──
    def trigger_github_actions(self):
        url = self.url_entry.get().strip()
        start_chap = self.entry_start.get().strip()
        end_chap = self.entry_end.get().strip()
        repo = self.entry_repo.get().strip()
        token = self.entry_token.get().strip()

        if not repo or not token:
            messagebox.showwarning("提示", "請填寫 GitHub Repo 與 Token 後再試！")
            return

        if not start_chap.isdigit() or not end_chap.isdigit():
            messagebox.showwarning("提示", "開始與結束章節必須為數字！")
            return

        self.btn_run.config(state=tk.DISABLED)
        self.progress_bar.start(10)
        self.lbl_status.config(text="雲端啟動中...", foreground="#e1b12c")
        self.log(f"🚀 正向 GitHub 雲端 (Repo: {repo}) 發動工作流 ...")
        self.log(f"   參數: 網址={url}, 章節={start_chap} ~ {end_chap}")

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
                    "ref": "main",
                    "inputs": {
                        "catalog_url": url,
                        "start_chap": start_chap,
                        "end_chap": end_chap
                    }
                }

                r = requests.post(dispatch_url, headers=headers, json=payload, timeout=15)
                if r.status_code != 24: # HTTP 204 No Content 代表成功
                    if r.status_code not in (200, 204):
                        raise Exception(f"GitHub API 回應錯誤 ({r.status_code}): {r.text}")

                self.root.after(0, lambda: self.log("✓ 成功觸發 GitHub Actions 工作流！等待雲端啟動..."))

                # 2. 開始輪詢 Workflow 狀態
                time.sleep(4)
                self._poll_workflow_runs(repo, token)

            except Exception as e:
                self.root.after(0, lambda: self._on_workflow_failed(str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _poll_workflow_runs(self, repo, token):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        runs_url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=1"

        run_id = None
        max_wait = 30
        for _ in range(max_wait):
            r = requests.get(runs_url, headers=headers, timeout=10)
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                if runs:
                    latest = runs[0]
                    run_id = latest["id"]
                    status = latest["status"]
                    self.root.after(0, lambda s=status: self.log(f"已連結至雲端 Run ID #{run_id}，目前狀態: {s}"))
                    break
            time.sleep(2)

        if not run_id:
            raise Exception("無法取得最新的 Workflow Run！請確認權限與 Workflow 檔案狀態。")

        # 持續追蹤 Jobs
        jobs_url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
        prev_steps = set()

        while True:
            r = requests.get(jobs_url, headers=headers, timeout=10)
            if r.status_code == 200:
                jobs = r.json().get("jobs", [])
                if jobs:
                    job = jobs[0]
                    job_status = job.get("status")
                    job_conclusion = job.get("conclusion")
                    steps = job.get("steps", [])

                    for step in steps:
                        s_name = step.get("name")
                        s_status = step.get("status")
                        s_conc = step.get("conclusion")
                        step_key = f"{s_name}_{s_status}_{s_conc}"

                        if step_key not in prev_steps:
                            prev_steps.add(step_key)
                            msg = f" └─ Step [{s_name}]: status={s_status}"
                            if s_conc:
                                msg += f" ({s_conc})"
                            self.root.after(0, lambda m=msg: self.log(m))

                    self.root.after(0, lambda s=job_status: self.lbl_status.config(text=f"雲端狀態: {s}", foreground="#2980b9"))

                    if job_status == "completed":
                        if job_conclusion == "success":
                            self.root.after(0, lambda: self._on_workflow_success(repo, run_id))
                        else:
                            self.root.after(0, lambda c=job_conclusion: self._on_workflow_failed(f"雲端執行失敗: {c}"))
                        break

            time.sleep(5)

    def _on_workflow_success(self, repo, run_id):
        self.progress_bar.stop()
        self.btn_run.config(state=tk.NORMAL)
        self.lbl_status.config(text="🎉 雲端製作完成！", foreground="#27ae60")
        self.log("==========================================")
        self.log(f"🎉 恭喜！GitHub Actions 雲端工作流成功執行完畢。")
        self.log(f"👉 請前往 https://github.com/{repo}/actions/runs/{run_id} 下載成品的 Artifacts 檔案！")
        self.log("==========================================")
        messagebox.showinfo("完成", f"雲端有聲小說製作成功！\n成品可由 GitHub Actions 的 Artifacts 下載。")

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
