"""
gui_merge_app.py — 雲端 GitHub Actions 全書單一 MP4 影片無損合併工具 (Cloud GUI)
"""

import os
import sys
import json
import time
import requests
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from dotenv import load_dotenv
import webbrowser

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

class MergeCloudAppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("☁️ 雲端 GitHub Actions 全書 MP4 合併與 GB 容量評估工具")
        self.root.geometry("780x680")
        self.root.minsize(700, 600)

        self.repo = os.getenv("GITHUB_REPO", "hub-google/audiobook-generator")
        self.token = os.getenv("GITHUB_TOKEN", "")
        self.current_run_id = None
        self.current_merge_run_id = None
        self.cancel_requested = False

        self._setup_style()
        self._build_ui()
        self.root.after(500, self.fetch_latest_runs)

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
        title_lbl = ttk.Label(main_frame, text="☁️ 雲端 GitHub Actions 全書 MP4 無損合併工具", style="Title.TLabel")
        title_lbl.pack(anchor=tk.W, pady=(0, 15))

        # ── 區塊 1: 雲端 Run ID 設定與選擇 ──
        sec_cloud = ttk.LabelFrame(main_frame, text="1. 雲端 Worker 產出物來源 (GitHub Actions Run)")
        sec_cloud.pack(fill=tk.X, pady=(0, 15))

        f_repo = ttk.Frame(sec_cloud)
        f_repo.pack(fill=tk.X, pady=3)
        ttk.Label(f_repo, text="GitHub Repo:", width=14).pack(side=tk.LEFT)
        self.entry_repo = ttk.Entry(f_repo, width=40)
        self.entry_repo.insert(0, self.repo)
        self.entry_repo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        ttk.Button(f_repo, text="🔄 重新整理 Run 列表", command=self.fetch_latest_runs).pack(side=tk.LEFT)

        # 選項卡：下拉選擇最近成功的 Run
        f_run = ttk.Frame(sec_cloud)
        f_run.pack(fill=tk.X, pady=5)
        ttk.Label(f_run, text="選擇來源 Run ID:", width=14).pack(side=tk.LEFT)
        self.cbo_runs = ttk.Combobox(f_run, state="readonly", width=55)
        self.cbo_runs.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        # ── 區塊 2: 雲端合併控管與結果 ──
        sec_action = ttk.LabelFrame(main_frame, text="2. 發動雲端合併與數據分析")
        sec_action.pack(fill=tk.BOTH, expand=True)

        f_act = ttk.Frame(sec_action)
        f_act.pack(fill=tk.X, pady=5)

        self.btn_run_merge = ttk.Button(f_act, text="🚀 發動 Action 雲端全書無損合併", style="Accent.TButton", command=self.trigger_cloud_merge)
        self.btn_run_merge.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_download = ttk.Button(f_act, text="📥 一鍵下載合併後 MP4", command=self.download_merged_artifact, state=tk.DISABLED)
        self.btn_download.pack(side=tk.LEFT, padx=(0, 10))

        self.lbl_status = ttk.Label(f_act, text="就緒", style="Status.TLabel")
        self.lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 進度條
        self.progress_bar = ttk.Progressbar(sec_action, mode="indeterminate")
        self.progress_bar.pack(fill=tk.X, pady=5)

        # 即時雲端 Log 控制台
        ttk.Label(sec_action, text="雲端執行日誌 (Action Logs):").pack(anchor=tk.W, pady=(5, 2))
        self.log_text = scrolledtext.ScrolledText(sec_action, height=14, background="#1e1e1e", foreground="#dcdcdc", font=("Consolas", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def get_headers(self):
        token = os.getenv("GITHUB_TOKEN", self.token)
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28"
        }

    def fetch_latest_runs(self):
        repo = self.entry_repo.get().strip()
        self.log(f"🔍 正在向 GitHub (https://github.com/{repo}) 查詢 Worker 產出物記錄...")
        
        def _worker():
            try:
                url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=20"
                r = requests.get(url, headers=self.get_headers(), timeout=10)
                if r.status_code != 200:
                    raise Exception(f"HTTP {r.status_code}: {r.text}")
                
                runs = r.json().get("workflow_runs", [])
                options = []
                for run in runs:
                    r_id = run["id"]
                    r_name = run["name"]
                    r_status = run["status"]
                    r_conclusion = run.get("conclusion") or r_status
                    created = run["created_at"].replace("T", " ").replace("Z", "")
                    
                    if "Audiobook Automation Pipeline" in r_name or "Merge" in r_name:
                        options.append(f"Run #{r_id} | {r_name} | {r_conclusion} ({created})")

                def _update():
                    self.cbo_runs["values"] = options
                    if options:
                        self.cbo_runs.current(0)
                        self.log(f"✓ 成功找到 {len(options)} 筆最近的 GitHub Actions 記錄！")
                    else:
                        self.log("⚠️ 未找到相關的 GitHub Actions 執行記錄。")

                self.root.after(0, _update)

            except Exception as e:
                self.root.after(0, lambda err=str(e): self.log(f"❌ 查詢 Actions 記錄失敗: {err}"))

        threading.Thread(target=_worker, daemon=True).start()

    def trigger_cloud_merge(self):
        repo = self.entry_repo.get().strip()
        selected_run_str = self.cbo_runs.get().strip()
        
        target_run_id = ""
        if selected_run_str:
            import re
            m = re.search(r'Run #(\d+)', selected_run_str)
            if m:
                target_run_id = m.group(1)

        self.btn_run_merge.config(state=tk.DISABLED)
        self.btn_download.config(state=tk.DISABLED)
        self.progress_bar.start(10)
        self.lbl_status.config(text="雲端發動中...", foreground="#e1b12c")
        self.log(f"🚀 向 GitHub (Repo: {repo}) 發動全書單一 MP4 合併工作流 ...")
        if target_run_id:
            self.log(f"   指定下載 Worker 產出物來源 Run ID: {target_run_id}")
        else:
            self.log("   未指定 Run ID，雲端將自動選取最新一次成功的 Worker 產出物")

        def _worker():
            try:
                dispatch_url = f"https://api.github.com/repos/{repo}/actions/workflows/test_merge_all.yml/dispatches"
                payload = {
                    "ref": "master",
                    "inputs": {
                        "run_id": target_run_id
                    }
                }
                r = requests.post(dispatch_url, headers=self.get_headers(), json=payload, timeout=15)
                if r.status_code not in (200, 204):
                    raise Exception(f"GitHub API 回應錯誤 ({r.status_code}): {r.text}")

                self.root.after(0, lambda: self.log("✓ 成功觸發 test_merge_all.yml 工作流！等待雲端啟動..."))
                time.sleep(4)
                self._poll_merge_workflow_runs(repo)

            except Exception as e:
                self.root.after(0, lambda err=str(e): self._on_merge_failed(err))

        threading.Thread(target=_worker, daemon=True).start()

    def _poll_merge_workflow_runs(self, repo):
        start_time = time.time()
        found_run = None

        while time.time() - start_time < 60:
            try:
                url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=10"
                r = requests.get(url, headers=self.get_headers(), timeout=10)
                if r.status_code == 200:
                    runs = r.json().get("workflow_runs", [])
                    for run in runs:
                        if run["name"] == "Test Merge All Chapters into Single MP4" and run["status"] in ("in_progress", "queued", "completed"):
                            found_run = run
                            break
                if found_run:
                    break
            except Exception:
                pass
            time.sleep(3)

        if not found_run:
            raise Exception("未能追蹤到剛啟動的全書合併 Action 任務，請確認 GitHub 專案存取權限。")

        run_id = found_run["id"]
        run_url = found_run["html_url"]
        self.current_merge_run_id = run_id
        self.root.after(0, lambda: self.log(f"🔗 已連結至雲端執行任務 [Run #{run_id}]: {run_url}"))

        while True:
            try:
                url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
                r = requests.get(url, headers=self.get_headers(), timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    status = data["status"]
                    conclusion = data.get("conclusion")

                    if status == "completed":
                        if conclusion == "success":
                            self.root.after(0, lambda: self._on_merge_success(run_id, repo))
                        else:
                            self.root.after(0, lambda c=conclusion: self._on_merge_failed(f"雲端工作流執行結果為 {c}"))
                        break
                    else:
                        self.root.after(0, lambda s=status: (
                            self.lbl_status.config(text=f"雲端合併中 ({s})...", foreground="#e1b12c"),
                            self.log(f"   └─ 雲端狀態: {s} ...")
                        ))
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.log(f"⚠️ 輪詢狀態時出現異常: {err}"))

            time.sleep(6)

    def _on_merge_success(self, run_id, repo):
        self.progress_bar.stop()
        self.btn_run_merge.config(state=tk.NORMAL)
        self.btn_download.config(state=tk.NORMAL)
        self.lbl_status.config(text="合併完成！", foreground="#27ae60")

        self.log("=" * 60)
        self.log("🎉🎉🎉 【全書單一 MP4 雲端合併完成】 🎉🎉🎉")
        self.log(f"  • GitHub Run ID: #{run_id}")
        self.log("  • 產出物 Artifact: full-book-single-mp4已就緒！")
        self.log("  • 請點擊【📥 一鍵下載合併後 MP4】即可將最終單一 MP4 下載回本機。")
        self.log("=" * 60)
        messagebox.showinfo("雲端合併完成", "GitHub Action 已經在雲端成功將全書 MP4 無損合併！\n點擊「一鍵下載合併後 MP4」即可將成品下載至本機。")

    def _on_merge_failed(self, err_msg):
        self.progress_bar.stop()
        self.btn_run_merge.config(state=tk.NORMAL)
        self.lbl_status.config(text="執行失敗", foreground="#e74c3c")
        self.log(f"❌ 雲端合併失敗: {err_msg}")
        messagebox.showerror("錯誤", f"發動雲端全書合併發生錯誤：\n{err_msg}")

    def download_merged_artifact(self):
        if not self.current_merge_run_id:
            messagebox.showwarning("提示", "目前沒有完成的雲端合併任務！")
            return

        repo = self.entry_repo.get().strip()
        run_id = self.current_merge_run_id
        self.log(f"📥 正在從 GitHub Actions (Run #{run_id}) 下載合併後 MP4 Artifact...")
        
        os.makedirs("Downloads", exist_ok=True)
        if sys.platform == "win32":
            os.startfile(os.path.abspath("Downloads"))

        def _download():
            try:
                cmd = ["gh", "run", "download", str(run_id), "--dir", "Downloads"]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res.returncode == 0:
                    self.root.after(0, lambda: self.log("✅ 成品已成功下載至 Downloads/ 資料夾！"))
                    self.root.after(0, lambda: messagebox.showinfo("下載完成", "全書單一 MP4 影片已下載至 Downloads/ 資料夾！"))
                else:
                    raise Exception(res.stderr)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self.log(f"❌ 下載失敗 (請確認已安裝 GitHub CLI 'gh'): {err}"))

        threading.Thread(target=_download, daemon=True).start()

if __name__ == "__main__":
    root = tk.Tk()
    app = MergeCloudAppGUI(root)
    root.mainloop()
