"""HF audiobook grouping and cloud merge/upload control panel."""
from __future__ import annotations

import json, os, shutil, subprocess, sys, threading, time, tkinter as tk, webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from hf_catalog import HfBook, HfCatalog, load_local_env
from merge_plan import build_plan, format_duration, format_size
from merge_status import collect_status

REPOSITORY, WORKFLOW = "hub-google/audiobook-generator", "merge-hf-book.yml"

def find_gh():
    fixed = Path(r"C:\Program Files\GitHub CLI\gh.exe")
    return str(fixed if fixed.exists() else shutil.which("gh") or fixed)

def run_gh(*args):
    result = subprocess.run([find_gh(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode: raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout

def resolve_hf_repo():
    if os.getenv("HF_ARCHIVE_REPO", "").strip(): return os.environ["HF_ARCHIVE_REPO"].strip()
    try:
        value = str(json.loads(run_gh("api", f"repos/{REPOSITORY}/actions/variables/HF_ARCHIVE_REPO")).get("value") or "").strip()
        if value: return value
    except Exception:
        pass
    from huggingface_hub import HfApi
    return f"{HfApi(token=os.getenv('HF_TOKEN')).whoami()['name']}/audiobook-archive"

def resolve_hf_delete_token():
    """Keep destructive HF access separate from the regular workflow token."""
    token = os.getenv("HF_DELETE_TOKEN", "").strip()
    if not token:
        raise ValueError("HF_DELETE_TOKEN 尚未設定；刪除資料夾必須使用獨立的寫入 Token")
    return token

BOOK_COLUMNS = {
    "title": "小說", "parts": "MP4 部數", "chapters": "章節",
    "duration": "MP4 總時長", "size": "總容量", "status": "完整性",
}

def book_sort_value(book: HfBook, column: str):
    """Return typed values so displayed units never cause lexical sorting."""
    first = book.parts[0].start_chapter if book.parts else -1
    last = book.parts[-1].end_chapter if book.parts else -1
    values = {
        "title": book.title.casefold(),
        "parts": len(book.parts),
        "chapters": (first, last),
        "duration": book.total_duration,
        "size": book.total_bytes,
        "status": (0 if book.mergeable else 1, book.error.casefold()),
    }
    return values[column]

class MergeUploadGUI(tk.Tk):
    def __init__(self):
        super().__init__(); load_local_env(ROOT)
        self.title("全集合併與兩階段上傳"); self.geometry("1380x920"); self.minsize(1080, 780)
        self.books, self.selected_book, self.current_plan, self.run_url = {}, None, None, ""
        self.status_rows, self._refresh_after = {}, None
        self.book_sort_column, self.book_sort_reverse = "title", False
        self._build(); self.after(250, self.refresh_books)

    def _build(self):
        top=ttk.Frame(self,padding=14); top.pack(fill="x")
        self.repo_var=tk.StringVar(value="正在讀取 HF_ARCHIVE_REPO…"); ttk.Label(top,textvariable=self.repo_var).pack(side="left")
        self.refresh_btn=ttk.Button(top,text="重新整理清單與進度",command=lambda:self.refresh_books(True)); self.refresh_btn.pack(side="right")
        self.scan_resume_btn=ttk.Button(top,text="立即掃描續傳排程",command=self.dispatch_resume_scheduler); self.scan_resume_btn.pack(side="right",padx=(0,8))
        progress=ttk.LabelFrame(self,text="已發動合併的書（第一階段：合併＋上傳至 98%；第二階段：24 小時後續傳＋發布）",padding=8); progress.pack(fill="x",padx=14,pady=(0,8))
        scols=("book","phase1_run","phase1","phase2_run","phase2","resume"); self.status_tree=ttk.Treeview(progress,columns=scols,show="headings",height=5)
        for key,label,width in (("book","小說",255),("phase1_run","第一階段 Run",120),("phase1","第一階段",220),("phase2_run","第二階段 Run",120),("phase2","第二階段",190),("resume","什麼時候會續做（台北時間）",285)):
            self.status_tree.heading(key,text=label); self.status_tree.column(key,width=width,anchor="w" if key in {"book","phase1","phase2","resume"} else "center")
        self.status_tree.pack(fill="x")
        self.status_tree.bind("<ButtonRelease-1>",self.open_status_run)
        self.status_tree.bind("<Motion>",self.status_link_cursor)
        box=ttk.LabelFrame(self,text="1. 選擇 HF 上可合併的小說",padding=10); box.pack(fill="both",expand=True,padx=14,pady=(0,10))
        book_actions=ttk.Frame(box); book_actions.pack(side="bottom",fill="x",pady=(8,0))
        self.delete_book_btn=ttk.Button(book_actions,text="刪除選取小說的 HF 資料夾",command=self.delete_selected_book,state="disabled")
        self.delete_book_btn.pack(side="right")
        book_table=ttk.Frame(box); book_table.pack(fill="both",expand=True)
        cols=tuple(BOOK_COLUMNS); self.book_tree=ttk.Treeview(book_table,columns=cols,show="headings",height=9)
        widths=(280,80,130,130,100,250)
        for key,width in zip(cols,widths):
            self.book_tree.heading(key,text=BOOK_COLUMNS[key],command=lambda column=key:self.sort_books(column)); self.book_tree.column(key,width=width,anchor="center")
        self._update_book_headings()
        self.book_tree.column("title",anchor="w"); scroll=ttk.Scrollbar(book_table,orient="vertical",command=self.book_tree.yview)
        self.book_tree.configure(yscrollcommand=scroll.set); self.book_tree.pack(side="left",fill="both",expand=True); scroll.pack(side="right",fill="y")
        self.book_tree.bind("<<TreeviewSelect>>",self.on_select_book)
        opts=ttk.LabelFrame(self,text="2. 選擇合併方式",padding=10); opts.pack(fill="x",padx=14,pady=(0,10))
        self.mode_var=tk.StringVar(value="hours"); ttk.Radiobutton(opts,text="每支影片最多",variable=self.mode_var,value="hours",command=self.update_preview).pack(side="left")
        self.hours_var=tk.StringVar(value="48"); entry=ttk.Entry(opts,textvariable=self.hours_var,width=8); entry.pack(side="left",padx=(8,4)); entry.bind("<KeyRelease>",self.update_preview)
        ttk.Label(opts,text="小時（依序放入最多 HF Parts，絕不超過）").pack(side="left")
        ttk.Radiobutton(opts,text="全部合併成一部",variable=self.mode_var,value="all",command=self.update_preview).pack(side="left",padx=(30,0))
        ttk.Label(opts,text="YouTube 隱私：").pack(side="left",padx=(30,4)); self.privacy_var=tk.StringVar(value="public")
        ttk.Combobox(opts,textvariable=self.privacy_var,values=("private","unlisted","public"),state="readonly",width=10).pack(side="left")
        preview=ttk.LabelFrame(self,text="3. 合併計畫預覽（每列會成為一支 YouTube 影片）",padding=10); preview.pack(fill="both",expand=True,padx=14,pady=(0,10))
        pcols=("output","youtube_title","parts","chapters","duration","size"); self.preview_tree=ttk.Treeview(preview,columns=pcols,show="headings",height=8)
        for key,label,width in (("output","輸出",70),("youtube_title","YouTube 影片名稱",520),("parts","HF Part 範圍",140),("chapters","章節範圍",130),("duration","精確時長",110),("size","來源總容量",110)):
            self.preview_tree.heading(key,text=label); self.preview_tree.column(key,width=width,anchor="center")
        self.preview_tree.column("youtube_title",anchor="w")
        preview_scroll=ttk.Scrollbar(preview,orient="horizontal",command=self.preview_tree.xview)
        self.preview_tree.configure(xscrollcommand=preview_scroll.set)
        self.preview_tree.pack(fill="both",expand=True); preview_scroll.pack(fill="x")
        bottom=ttk.Frame(self,padding=(14,0,14,12)); bottom.pack(fill="x")
        self.start_btn=ttk.Button(bottom,text="送出 GitHub Actions 合併＋98% 上傳",command=self.start,state="disabled"); self.start_btn.pack(side="left")
        self.open_btn=ttk.Button(bottom,text="開啟 Actions Run",command=lambda:webbrowser.open(self.run_url),state="disabled"); self.open_btn.pack(side="left",padx=8)
        self.status_var=tk.StringVar(value="正在載入…"); ttk.Label(bottom,textvariable=self.status_var).pack(side="right")
        self.detail_var=tk.StringVar(); ttk.Label(self,textvariable=self.detail_var,padding=(14,0,14,12),foreground="#555").pack(fill="x")

    def dispatch_resume_scheduler(self):
        self.scan_resume_btn.configure(state="disabled")
        self.status_var.set("正在觸發二階段續傳掃描排程…")
        def _run():
            try:
                run_gh("workflow", "run", "hf-upload-resume-scheduler.yml")
                self.after(0, lambda: self.status_var.set("已發動續傳排程掃描！稍後點擊重新整理可查看進度。"))
                self.after(3000, lambda: self.refresh_books(True))
            except Exception as exc:
                self.after(0, self._show_error, f"觸發續傳排程失敗：{type(exc).__name__}: {exc}")
            finally:
                self.after(4000, lambda: self.scan_resume_btn.configure(state="normal"))
        threading.Thread(target=_run, daemon=True).start()

    def refresh_books(self,force=False):
        if self._refresh_after:
            self.after_cancel(self._refresh_after); self._refresh_after=None
        self.refresh_btn.configure(state="disabled"); self.status_var.set("正在掃描 HF 書庫…" if force else "正在載入 HF 書庫快取…")
        threading.Thread(target=self._load_books,args=(force,),daemon=True).start()
    def _schedule_refresh(self):
        if self._refresh_after:
            self.after_cancel(self._refresh_after)
        self._refresh_after=self.after(60_000,self.refresh_books)
    def _load_books(self,force=False):
        try:
            token=os.getenv("HF_TOKEN","").strip(); repo=resolve_hf_repo(); books=HfCatalog(repo,token).list_books(force_refresh=force)
            statuses=collect_status(repo,token,run_gh)
            self.after(0,self._show_books,repo,books,statuses)
        except Exception as exc: self.after(0,self._show_error,f"HF 清單讀取失敗：{type(exc).__name__}: {exc}")
    def _show_books(self,repo,books,statuses=None):
        self.repo_var.set(repo); self.books={b.key:b for b in books}; self.book_tree.delete(*self.book_tree.get_children())
        ordered=sorted(books,key=lambda book:book_sort_value(book,self.book_sort_column),reverse=self.book_sort_reverse)
        for book in ordered:
            chapters=f"{book.parts[0].start_chapter}–{book.parts[-1].end_chapter}" if book.parts else "—"
            self.book_tree.insert("","end",iid=book.key,values=(book.title,len(book.parts),chapters,format_duration(book.total_duration),format_size(book.total_bytes),"可合併" if book.mergeable else f"不可合併：{book.error}"))
        self.status_tree.delete(*self.status_tree.get_children()); self.status_rows={}
        for index,row in enumerate(statuses or []):
            iid=f"status-{index}"; self.status_rows[iid]=row
            self.status_tree.insert("","end",iid=iid,values=(row["title"],"🔗 開啟合併 Run" if row.get("phase1_run_url") else "—",row["phase1"],"🔗 開啟續傳 Run" if row.get("phase2_run_url") else "尚未派送",row["phase2"],row["resume"]))
        self.status_var.set(f"共 {len(books)} 本；{len(statuses or [])} 本有合併紀錄"); self.refresh_btn.configure(state="normal")
        self._schedule_refresh()
    def _update_book_headings(self):
        for key,label in BOOK_COLUMNS.items():
            arrow=(" ▼" if self.book_sort_reverse else " ▲") if key==self.book_sort_column else ""
            self.book_tree.heading(key,text=label+arrow)
    def sort_books(self,column):
        if self.book_sort_column==column: self.book_sort_reverse=not self.book_sort_reverse
        else: self.book_sort_column,self.book_sort_reverse=column,False
        self._update_book_headings()
        selected=set(self.book_tree.selection())
        ordered=sorted(self.books.values(),key=lambda book:book_sort_value(book,column),reverse=self.book_sort_reverse)
        for index,book in enumerate(ordered): self.book_tree.move(book.key,"",index)
        if selected: self.book_tree.selection_set([key for key in selected if key in self.books])
    def open_status_run(self,_event=None):
        item=self.status_tree.identify_row(_event.y) if _event else ""
        column=self.status_tree.identify_column(_event.x) if _event else ""
        if not item or column not in {"#2", "#4"}:
            return
        row=self.status_rows.get(item,{}); url=row.get("phase2_run_url") if column == "#4" else row.get("phase1_run_url")
        if url:
            webbrowser.open(url)
    def status_link_cursor(self,event):
        item=self.status_tree.identify_row(event.y); column=self.status_tree.identify_column(event.x)
        row=self.status_rows.get(item,{})
        linked=(column == "#2" and row.get("phase1_run_url")) or (column == "#4" and row.get("phase2_run_url"))
        self.status_tree.configure(cursor="hand2" if linked else "")
    def _show_error(self,message):
        self.status_var.set(message); self.refresh_btn.configure(state="normal"); self._schedule_refresh(); messagebox.showerror("錯誤",message)
    def on_select_book(self,_event=None):
        selected=self.book_tree.selection(); self.selected_book=self.books.get(selected[0]) if selected else None
        self.delete_book_btn.configure(state="normal" if self.selected_book else "disabled"); self.update_preview()
    def delete_selected_book(self):
        book=self.selected_book
        if not book: return
        message=(f"將永久刪除 Hugging Face 上的整本小說資料夾：\n\n《{book.title}》\n{book.root}\n\n"
                 "其中所有 MP4、JSON 與其他檔案都會刪除。此動作無法由本程式復原，確定繼續？")
        if not messagebox.askyesno("確認刪除 HF 小說資料夾",message,icon="warning"): return
        if self._refresh_after:
            self.after_cancel(self._refresh_after); self._refresh_after=None
        self.delete_book_btn.configure(state="disabled"); self.refresh_btn.configure(state="disabled")
        self.status_var.set(f"正在刪除《{book.title}》的 HF 資料夾…")
        threading.Thread(target=self._delete_book,args=(book,),daemon=True).start()
    def _delete_book(self,book):
        try:
            token=resolve_hf_delete_token(); repo=resolve_hf_repo(); HfCatalog(repo,token).delete_book(book)
            self.after(0,self._book_deleted,book)
        except Exception as exc:
            self.after(0,self._delete_book_error,f"HF 資料夾刪除失敗：{type(exc).__name__}: {exc}")
    def _delete_book_error(self,message):
        self.delete_book_btn.configure(state="normal" if self.selected_book else "disabled")
        self._show_error(message)
    def _book_deleted(self,book):
        self.selected_book=None; self.current_plan=None; self.delete_book_btn.configure(state="disabled")
        self.status_var.set(f"已刪除《{book.title}》，正在重新掃描 HF 書庫…")
        self.refresh_books(True)
    def update_preview(self,_event=None):
        self.preview_tree.delete(*self.preview_tree.get_children()); self.current_plan=None; book=self.selected_book
        if not book: self.detail_var.set("請先選擇一本小說。"); self.start_btn.configure(state="disabled"); return
        if not book.mergeable: self.detail_var.set(book.error); self.start_btn.configure(state="disabled"); return
        try:
            plan=build_plan(book,None if self.mode_var.get()=="all" else float(self.hours_var.get()))
            for item in plan["outputs"]: self.preview_tree.insert("","end",values=(f"第 {item['output_number']} 支",item["youtube_title"],f"Part {item['part_start']:02d}–{item['part_end']:02d}",f"Ch {item['start_chapter']}–{item['end_chapter']}",format_duration(item["duration_seconds"]),format_size(item["bytes"])))
            self.current_plan=plan; self.detail_var.set(f"計畫 {plan['plan_id']}｜{len(book.parts)} 個 HF MP4 → {len(plan['outputs'])} 支影片｜總時長 {format_duration(book.total_duration)}"); self.start_btn.configure(state="normal")
        except (ValueError,TypeError) as exc: self.detail_var.set(str(exc)); self.start_btn.configure(state="disabled")
    def start(self):
        if not self.current_plan or not messagebox.askyesno("確認送出",f"將建立 {len(self.current_plan['outputs'])} 支影片。\n合併及兩階段上傳均在 GitHub Actions 執行，確定送出？"): return
        self.start_btn.configure(state="disabled"); self.status_var.set("正在送出 GitHub Actions…"); threading.Thread(target=self._dispatch,daemon=True).start()
    def _dispatch(self):
        try:
            p=self.current_plan; before=datetime.now(timezone.utc); fields=[]
            for key,value in (("book_key",p["book_key"]),("book_title",p["book_title"]),("repo_revision",p["repo_revision"]),("merge_mode",p["mode"]),("max_hours",p.get("max_hours") or ""),("expected_plan_id",p["plan_id"]),("privacy",self.privacy_var.get())): fields += ["-f",f"{key}={value}"]
            run_gh("workflow","run",WORKFLOW,"--repo",REPOSITORY,*fields); run=self._find_run(before); self.run_url=run["url"]
            self.after(0,lambda:self.open_btn.configure(state="normal")); self.after(0,self.status_var.set,f"已送出 Run #{run['databaseId']}")
            self.after(0,lambda:self.refresh_books(True))
        except Exception as exc: self.after(0,self._show_error,f"送出失敗：{type(exc).__name__}: {exc}")
        finally: self.after(0,lambda:self.start_btn.configure(state="normal" if self.current_plan else "disabled"))
    def _find_run(self,after):
        for _ in range(30):
            runs=json.loads(run_gh("run","list","--repo",REPOSITORY,"--workflow",WORKFLOW,"--event","workflow_dispatch","--limit","10","--json","databaseId,createdAt,url"))
            for item in runs:
                if datetime.fromisoformat(item["createdAt"].replace("Z","+00:00"))>=after:return item
            time.sleep(2)
        raise RuntimeError("workflow 已送出，但 60 秒內找不到新 Run")

if __name__=="__main__": MergeUploadGUI().mainloop()
