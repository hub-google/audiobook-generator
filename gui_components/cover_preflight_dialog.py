"""Single-book preflight image/AI analysis review controls."""
from .runtime import *  # noqa: F401,F403

class CoverPreflightDialogMixin:
    def open_cover_preflight_review(self):
        task = self._selected_task()
        if not task or not task.get("cover_run_id"):
            messagebox.showinfo("封面審核", "封面 Run 完成後才能查看。")
            return
        top = tk.Toplevel(self.root); top.title(f"封面圖與小說介紹／HF Prompt｜《{task.get('book_title')}》")
        top.geometry("1440x900"); top.minsize(1120, 720)
        status = tk.StringVar(value="正在下載封面審核資料…"); ttk.Label(top, textvariable=status, padding=8).pack(fill=tk.X)
        panes = ttk.Panedwindow(top, orient=tk.HORIZONTAL); panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        image_frame = ttk.Frame(panes)
        upload_status = tk.StringVar(value="正在載入本次產生的封面…")
        upload_hint = ttk.Label(image_frame, textvariable=upload_status, wraplength=600, anchor=tk.CENTER)
        upload_hint.pack(fill=tk.X, padx=8, pady=(8, 4))
        image_label = ttk.Label(image_frame, text="載入中…", anchor=tk.CENTER)
        image_label.pack(fill=tk.BOTH, expand=True)
        upload_button = ttk.Button(image_frame, text="選擇圖片／更換封面", state=tk.DISABLED)
        upload_button.pack(pady=(4, 8))
        info_frame = ttk.Frame(panes)
        info_tabs = ttk.Notebook(info_frame)
        info_tabs.pack(fill=tk.BOTH, expand=True)
        analysis_text = scrolledtext.ScrolledText(info_tabs, wrap=tk.WORD, font=("Microsoft JhengHei", 10))
        prompt_text = scrolledtext.ScrolledText(info_tabs, wrap=tk.WORD, font=("Consolas", 10))
        info_tabs.add(analysis_text, text="Gemini 小說介紹／視覺分析")
        info_tabs.add(prompt_text, text="HF 生圖 Prompt")
        panes.add(image_frame, weight=5); panes.add(info_frame, weight=7)

        upload_in_progress = {"value": False}
        review_ready = {"value": False}

        def current_task():
            if not self.cloud_queue:
                return task
            return next((item for item in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", [])
                         if item.get("task_id") == task.get("task_id")), task)

        def manual_override_allowed():
            return current_task().get("workflow_phase") == "preflight"

        def show_preview(image):
            shown = image.copy()
            shown.thumbnail((620, 820))
            photo = ImageTk.PhotoImage(shown)
            image_label.config(image=photo, text="", cursor="hand2")
            image_label.image = photo

        def finish_upload_controls():
            upload_in_progress["value"] = False
            upload_button.config(state=tk.NORMAL if manual_override_allowed() else tk.DISABLED)
            if hasattr(self, "btn_start_processing"):
                self._update_queue_control_states(self._selected_tasks())

        def process_upload(path):
            if not review_ready["value"]:
                upload_status.set("請等待本次封面審核資料載入完成後再更換圖片。")
                return
            if upload_in_progress["value"]:
                return
            if not manual_override_allowed():
                messagebox.showinfo(
                    "手動封面", "第二階段已經開始；這次執行的封面已鎖定，請重新排程後再更換。", parent=top,
                )
                return
            title = task.get("book_title") or "待解析"
            catalog_url = (task.get("catalog_url") or "").strip()
            if not catalog_url:
                messagebox.showerror("手動封面", "這項任務缺少小說來源網址，無法綁定手動封面。", parent=top)
                return
            upload_in_progress["value"] = True
            upload_button.config(state=tk.DISABLED)
            if hasattr(self, "btn_start_processing"):
                self.btn_start_processing.config(state=tk.DISABLED)
            upload_status.set(f"正在為《{title}》檢查、裁切、壓縮並同步封面…")

            def upload_worker():
                try:
                    local_cover, profile_id, repo, details, cloud = self._upload_manual_cover(
                        title, catalog_url, path,
                    )

                    def done():
                        try:
                            with Image.open(local_cover) as image:
                                show_preview(image)
                            upload_status.set(
                                f"✓ 已改用手動封面｜1280×720 JPEG｜{details['bytes']/1024:.0f} KB｜"
                                f"GitHub: {repo}@{cloud['branch']}/{cloud['remote_path']}"
                            )
                            status.set("手動封面已同步；右側 Gemini 分析與 HF Prompt 保持不變。")
                            self.log(f"✓ 《{title}》手動封面已同步；第二階段將優先使用這張圖片。")
                        finally:
                            finish_upload_controls()

                    self.root.after(0, done)
                except Exception as error:
                    def failed(detail=str(error)):
                        upload_status.set("手動封面上傳失敗；仍保留原本產生的封面。")
                        messagebox.showerror("手動封面", detail, parent=top)
                        finish_upload_controls()
                    self.root.after(0, failed)

            threading.Thread(target=upload_worker, daemon=True).start()

        def choose_upload():
            if not review_ready["value"] or upload_in_progress["value"]:
                return
            path = filedialog.askopenfilename(parent=top, filetypes=[
                ("圖片", "*.jpg *.jpeg *.png *.webp"), ("所有檔案", "*.*"),
            ])
            if path:
                process_upload(path)

        upload_button.config(command=choose_upload)
        image_label.bind("<Button-1>", lambda _event: choose_upload() if manual_override_allowed() else None)

        def accept_drop(event):
            paths = top.tk.splitlist(event.data)
            if paths:
                process_upload(paths[0])
            return "break"

        drop_enabled = self._register_file_drop(
            (image_frame, upload_hint, image_label, upload_button), accept_drop,
        )

        def worker():
            try:
                files = self._download_artifact_files(task["cover_run_id"], f"cover-review-{task['task_id']}")
                image_data = next(data for name, data in files.items() if name.endswith("master_cover.jpg"))
                prompt_data = next(data for name, data in files.items() if name.endswith("master_cover_prompt.json"))
                record = json.loads(prompt_data.decode("utf-8"))
                with Image.open(io.BytesIO(image_data)) as pil:
                    preview_image = pil.copy()
                brief = record.get("brief") if isinstance(record.get("brief"), dict) else record
                def render():
                    review_ready["value"] = True
                    show_preview(preview_image)
                    analysis_text.insert("1.0", self._format_cover_analysis(brief))
                    prompt_text.insert("1.0", record.get("prompt") or brief.get("prompt", ""))
                    status.set("封面圖、Gemini 小說介紹與 HF 生圖 Prompt 已分開載入。")
                    if manual_override_allowed():
                        upload_button.config(state=tk.NORMAL)
                        upload_status.set(
                            "目前顯示本次產生的封面；可將 JPG／PNG／WEBP 拖曳到左側，或選擇圖片覆蓋。"
                            if drop_enabled else
                            "目前顯示本次產生的封面；可按「選擇圖片／更換封面」覆蓋。"
                        )
                    else:
                        upload_status.set("第二階段已開始，這次執行的封面已鎖定。")
                self.root.after(0, render)
            except Exception as error:
                def failed(detail=str(error)):
                    review_ready["value"] = True
                    status.set(f"載入失敗：{detail}")
                    if manual_override_allowed():
                        upload_button.config(state=tk.NORMAL)
                        upload_status.set("原封面載入失敗；仍可選擇或拖曳圖片作為手動封面。")
                self.root.after(0, failed)
        threading.Thread(target=worker, daemon=True).start()

