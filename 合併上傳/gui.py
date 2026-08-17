"""Local control-panel GUI for the cloud merge/upload workflow."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

from merge_upload import normalize_run_id

REPOSITORY = "hub-google/audiobook-generator"
WORKFLOW = "merge-run-upload.yml"

def find_gh() -> str:
    found = shutil.which("gh")
    if found:
        return found
    installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if installed.exists():
        return str(installed)
    raise FileNotFoundError("找不到 GitHub CLI。請先安裝 gh 並執行 gh auth login。")

def run_gh(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        [find_gh(), *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout

class MergeUploadGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("有聲書 Run Artifacts 合併上傳")
        self.geometry("900x680")
        self.minsize(760, 560)
        self.run_url = ""
        self.stop_event = threading.Event()
        self._build()

    def _build(self):
        form = ttk.Frame(self, padding=16); form.pack(fill="x")
        self.vars = {
            "source_run_id": tk.StringVar(), "privacy": tk.StringVar(value="public"),
            "checkpoint_repo": tk.StringVar(),
        }
        labels = [("來源 Run ID", "source_run_id"),
                  ("HF checkpoint repo（可留空）", "checkpoint_repo")]
        for row, (label, key) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            ttk.Entry(form, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="YouTube 隱私").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Combobox(form, textvariable=self.vars["privacy"], values=("private", "unlisted", "public"), state="readonly").grid(row=2, column=1, sticky="w", pady=6)
        form.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self, padding=(16, 0, 16, 10)); buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="開始雲端合併上傳", command=self.start); self.start_button.pack(side="left")
        ttk.Button(buttons, text="停止監看", command=self.stop_monitor).pack(side="left", padx=8)
        self.open_button = ttk.Button(buttons, text="開啟 Actions Run", command=self.open_run, state="disabled"); self.open_button.pack(side="left")
        self.status_var = tk.StringVar(value="就緒")
        ttk.Label(buttons, textvariable=self.status_var).pack(side="right")
        self.progress = ttk.Progressbar(self, mode="indeterminate"); self.progress.pack(fill="x", padx=16)
        self.log = tk.Text(self, wrap="word", font=("Consolas", 10)); self.log.pack(fill="both", expand=True, padx=16, pady=12)
        self.log.insert("end", "貼上完整的 GitHub Actions Run 網址或純數字 Run ID；書名與原始封面會從該 Run 自動讀取。影片處理在雲端進行，本機不下載 MP4。\n")

    def append(self, message: str):
        self.after(0, lambda: (self.log.insert("end", message.rstrip() + "\n"), self.log.see("end")))

    def set_status(self, value: str): self.after(0, self.status_var.set, value)

    def start(self):
        try:
            run_id = normalize_run_id(self.vars["source_run_id"].get())
        except (argparse.ArgumentTypeError, TypeError, ValueError):
            messagebox.showerror(
                "輸入錯誤",
                "請貼上 Run ID 數字，或完整的 GitHub Actions Run 網址。",
            )
            return
        self.payload = {key: variable.get().strip() for key, variable in self.vars.items()}
        self.payload["source_run_id"] = run_id
        self.stop_event.clear(); self.start_button.configure(state="disabled"); self.progress.start(12)
        self.log.delete("1.0", "end")
        threading.Thread(target=self._dispatch_and_monitor, daemon=True).start()

    def _dispatch_and_monitor(self):
        try:
            run_gh("auth", "status")
            before = datetime.now(timezone.utc)
            fields = []
            for key in ("source_run_id", "privacy", "checkpoint_repo"):
                fields.extend(("-f", f"{key}={self.payload[key]}"))
            self.set_status("正在送出 workflow…"); self.append("正在送出 GitHub Actions workflow…")
            run_gh("workflow", "run", WORKFLOW, "--repo", REPOSITORY, *fields)
            action_run = self._find_new_run(before)
            database_id = str(action_run["databaseId"]); self.run_url = action_run["url"]
            self.after(0, lambda: self.open_button.configure(state="normal"))
            self.append(f"已建立 Actions run #{database_id}\n{self.run_url}")
            self._monitor(database_id)
        except Exception as error:
            self.set_status("失敗"); self.append(f"\n失敗：{type(error).__name__}: {error}")
        finally:
            self.after(0, lambda: (self.progress.stop(), self.start_button.configure(state="normal")))

    def _find_new_run(self, before):
        for _ in range(30):
            runs = json.loads(run_gh("run", "list", "--repo", REPOSITORY, "--workflow", WORKFLOW, "--event", "workflow_dispatch", "--limit", "10", "--json", "databaseId,createdAt,status,conclusion,url"))
            for item in runs:
                created = datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00"))
                if created >= before:
                    return item
            time.sleep(2)
        raise RuntimeError("已送出 workflow，但 60 秒內找不到對應的 Actions run。")

    def _monitor(self, database_id: str):
        previous = ""
        while not self.stop_event.is_set():
            data = json.loads(run_gh("run", "view", database_id, "--repo", REPOSITORY, "--json", "status,conclusion,url,jobs"))
            jobs = data.get("jobs") or []
            active = []
            for job in jobs:
                for step in job.get("steps") or []:
                    if step.get("status") == "in_progress": active.append(step.get("name", ""))
            snapshot = f"狀態: {data['status']} / {data.get('conclusion') or '-'}" + (f" | 目前步驟: {', '.join(active)}" if active else "")
            if snapshot != previous: self.append(snapshot); previous = snapshot
            self.set_status(snapshot)
            if data["status"] == "completed":
                if data.get("conclusion") == "success": self.append(f"\n執行成功。\n{data['url']}")
                else:
                    failed = run_gh("run", "view", database_id, "--repo", REPOSITORY, "--log-failed", check=False)
                    self.append(f"\n執行失敗，以下是 failed step log：\n{failed or '沒有取得 failed log，請開啟 Actions run 查看 Summary。'}")
                return
            time.sleep(15)
        self.set_status("已停止監看（雲端作業仍繼續）")

    def stop_monitor(self): self.stop_event.set()
    def open_run(self):
        if self.run_url: webbrowser.open(self.run_url)

if __name__ == "__main__": MergeUploadGUI().mainloop()
