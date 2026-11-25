"""Multi-book cover-information review and upload dialog."""
from .runtime import *  # noqa: F401,F403

class BatchCoverDialogMixin:
    def open_batch_cover_information(self):
        """Run the existing cover-information pipeline concurrently for selected queue rows."""
        tasks = self._selected_tasks()
        if not tasks:
            messagebox.showinfo("批次封面資訊", "請先在上方佇列選取一本或多本小說。")
            return

        top = tk.Toplevel(self.root)
        top.title(f"批次封面資訊＋手動封面｜{len(tasks)} 本")
        top.geometry("1000x720")
        top.minsize(760, 520)
        top.transient(self.root)

        status_var = tk.StringVar(value=f"準備處理 {len(tasks)} 本小說…")
        ttk.Label(top, textvariable=status_var).pack(fill=tk.X, padx=10, pady=(10, 5))
        progress = ttk.Progressbar(top, maximum=len(tasks), mode="determinate")
        progress.pack(fill=tk.X, padx=10, pady=(0, 8))

        body = ttk.Panedwindow(top, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        list_frame, output_frame = ttk.Frame(body), ttk.Frame(body)
        body.add(list_frame, weight=1)
        body.add(output_frame, weight=3)

        result_tree = ttk.Treeview(list_frame, columns=("book", "status"), show="headings", selectmode="browse")
        result_tree.heading("book", text="小說")
        result_tree.heading("status", text="結果")
        result_tree.column("book", width=150, anchor=tk.W)
        result_tree.column("status", width=90, anchor=tk.CENTER)
        result_tree.pack(fill=tk.BOTH, expand=True)
        for task in tasks:
            result_tree.insert("", tk.END, iid=task["task_id"], values=(task.get("book_title") or "待解析", "準備啟動"))

        output_tabs = ttk.Notebook(output_frame)
        output_tabs.pack(fill=tk.BOTH, expand=True)
        analysis_text = scrolledtext.ScrolledText(output_tabs, wrap=tk.WORD)
        prompt_text = scrolledtext.ScrolledText(output_tabs, wrap=tk.WORD, font=("Consolas", 10))
        upload_page = ttk.Frame(output_tabs, padding=10)
        output_tabs.add(analysis_text, text="小說架構／視覺分析")
        output_tabs.add(prompt_text, text="HF 生圖 Prompt")
        output_tabs.add(upload_page, text="上傳封面圖片")
        results = {}
        task_map = {task["task_id"]: task for task in tasks}

        upload_status = tk.StringVar(value="選取左側小說後，可選擇或拖曳 JPG／PNG／WEBP。")
        upload_hint = ttk.Label(upload_page, textvariable=upload_status, wraplength=680)
        upload_hint.pack(anchor=tk.W, pady=(0, 8))
        preview = ttk.Label(upload_page, anchor=tk.CENTER, text="將圖片拖曳到這裡")
        preview.pack(fill=tk.BOTH, expand=True, pady=8)

        def show_result(_event=None):
            selected = result_tree.selection()
            if not selected:
                return
            data = results.get(selected[0]) or {}
            analysis_text.delete("1.0", tk.END)
            prompt_text.delete("1.0", tk.END)
            analysis_text.insert("1.0", self._format_cover_analysis(data) if data.get("analysis") else data.get("error") or "尚未產生")
            prompt_text.insert("1.0", data.get("prompt") or data.get("error") or "尚未產生")
            task = task_map[selected[0]]
            try:
                local_cover = cache_path(PROJECT_ROOT, book_profile_id(task.get("catalog_url") or ""))
                if local_cover.is_file():
                    with Image.open(local_cover) as image:
                        shown = image.copy(); shown.thumbnail((640, 360))
                    preview.image = ImageTk.PhotoImage(shown)
                    preview.config(image=preview.image, text="")
                    upload_status.set(f"已讀取《{task.get('book_title')}》手動封面｜{local_cover.stat().st_size/1024:.0f} KB")
                else:
                    preview.config(image="", text="將圖片拖曳到這裡")
                    preview.image = None
                    upload_status.set(f"《{task.get('book_title')}》尚未上傳手動封面。")
            except (OSError, ValueError):
                preview.config(image="", text="將圖片拖曳到這裡")

        result_tree.bind("<<TreeviewSelect>>", show_result)
        first_id = tasks[0]["task_id"]
        result_tree.selection_set(first_id)
        show_result()

        actions = ttk.Frame(top)
        actions.pack(fill=tk.X, padx=10, pady=(0, 10))

        def copy_current():
            if output_tabs.index("current") == 2:
                return
            widget = analysis_text if output_tabs.index("current") == 0 else prompt_text
            value = widget.get("1.0", tk.END).strip()
            if value:
                top.clipboard_clear()
                top.clipboard_append(value)
                status_var.set("已複製目前頁面。")

        def copy_all():
            blocks = []
            for task in tasks:
                data = results.get(task["task_id"]) or {}
                if data.get("analysis") and data.get("prompt"):
                    title = task.get("book_title") or "待解析"
                    blocks.append(f"《{title}》\n\n【小說架構／視覺分析】\n{self._format_cover_analysis(data)}\n\n【HF 生圖 Prompt】\n{data['prompt']}")
            if blocks:
                top.clipboard_clear()
                top.clipboard_append("\n\n" + ("\n\n" + "=" * 60 + "\n\n").join(blocks))
                status_var.set(f"已複製 {len(blocks)} 本小說的全部結果。")

        ttk.Button(actions, text="複製目前頁面", command=copy_current).pack(side=tk.LEFT)
        copy_all_button = ttk.Button(actions, text="複製全部結果", command=copy_all, state=tk.DISABLED)
        copy_all_button.pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="關閉", command=top.destroy).pack(side=tk.RIGHT)

        def selected_upload_task():
            selected = result_tree.selection()
            return task_map.get(selected[0]) if selected else None

        def process_upload(path):
            task = selected_upload_task()
            if not task:
                return
            title, catalog_url = task.get("book_title") or "待解析", (task.get("catalog_url") or "").strip()
            upload_button.config(state=tk.DISABLED)
            upload_status.set(f"正在為《{title}》檢查、裁切、壓縮並同步封面…")
            def upload_worker():
                try:
                    local_cover, profile_id, repo, details, cloud = self._upload_manual_cover(title, catalog_url, path)
                    def done():
                        upload_button.config(state=tk.NORMAL)
                        if selected_upload_task() is task:
                            with Image.open(local_cover) as image:
                                shown = image.copy(); shown.thumbnail((640, 360))
                            preview.image = ImageTk.PhotoImage(shown)
                            preview.config(image=preview.image, text="")
                            upload_status.set(
                                f"✓ 上傳成功｜1280×720 JPEG｜{details['bytes']/1024:.0f} KB｜"
                                f"指紋 {profile_id}｜GitHub: {repo}@{cloud['branch']}/{cloud['remote_path']}"
                            )
                        self.log(f"✓ 《{title}》手動封面已同步；正式流程將跳過 Gemini 與 HF 生圖。")
                    post(done)
                except Exception as error:
                    post(lambda detail=str(error): (
                        upload_button.config(state=tk.NORMAL), upload_status.set("上傳失敗"),
                        messagebox.showerror("手動封面", detail, parent=top),
                    ))
            threading.Thread(target=upload_worker, daemon=True).start()

        def choose_upload():
            path = filedialog.askopenfilename(parent=top, filetypes=[
                ("圖片", "*.jpg *.jpeg *.png *.webp"), ("所有檔案", "*.*"),
            ])
            if path:
                process_upload(path)

        upload_button = ttk.Button(upload_page, text="選擇圖片檔案", command=choose_upload)
        upload_button.pack(pady=5)

        def accept_drop(event):
            paths = top.tk.splitlist(event.data)
            if paths:
                process_upload(paths[0])
            return "break"

        if self._register_file_drop((upload_page, upload_hint, preview, upload_button), accept_drop):
            upload_status.set("選取左側小說後，可選擇或拖曳 JPG／PNG／WEBP；將標準化為 1280×720 JPEG。")
        else:
            upload_status.set("目前環境未載入拖放元件，請先使用「選擇圖片檔案」。")

        def post(callback):
            def safe_callback():
                if top.winfo_exists():
                    callback()
            self.root.after(0, safe_callback)

        def worker():
            completed_count = 0
            success_count = 0
            lock = threading.Lock()

            def run_one(task):
                task_id = task["task_id"]
                title = task.get("book_title") or "待解析"
                catalog_url = (task.get("catalog_url") or "").strip()
                try:
                    if not catalog_url:
                        raise ValueError("佇列任務缺少目錄網址")
                    def stage(value):
                        post(lambda tid=task_id, text=value: result_tree.set(tid, "status", text))
                    data, cache_hit = self._load_or_generate_cover_information(
                        title, catalog_url, progress_callback=stage,
                    )
                    results[task_id] = data
                    label = "已讀快取" if cache_hit else "完成"
                    post(lambda tid=task_id, value=label: (result_tree.set(tid, "status", value), show_result()))
                    return True
                except Exception as error:
                    results[task_id] = {"error": f"產生失敗：{error}"}
                    post(lambda tid=task_id: (result_tree.set(tid, "status", "失敗"), show_result()))
                    return False

            post(lambda: (
                [result_tree.set(task["task_id"], "status", "啟動中") for task in tasks],
                status_var.set(f"已同時啟動 {len(tasks)} 本｜完成 0/{len(tasks)}"),
            ))
            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                future_map = {executor.submit(run_one, task): task for task in tasks}
                for future in as_completed(future_map):
                    succeeded = future.result()
                    with lock:
                        completed_count += 1
                        success_count += int(succeeded)
                        done, ok = completed_count, success_count
                    post(lambda value=done, good=ok: (
                        progress.configure(value=value),
                        status_var.set(
                            f"完成 {value}/{len(tasks)}｜執行中 {len(tasks)-value}｜成功 {good}｜失敗 {value-good}"
                        ),
                    ))
            post(lambda: (
                status_var.set(f"批次完成：成功 {success_count} 本，失敗 {len(tasks) - success_count} 本。"),
                copy_all_button.config(state=tk.NORMAL if success_count else tk.DISABLED),
            ))

        threading.Thread(target=worker, daemon=True).start()

