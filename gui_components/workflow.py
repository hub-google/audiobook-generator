from .runtime import (
    os, json, time, requests, threading, tk, ttk, messagebox,
    scrolledtext, filedialog, re, webbrowser, Image, ImageTk,
    book_profile_id, cache_path, restore_cover, PROJECT_ROOT,
)


from .chapter_editor import ChapterEditorMixin

class WorkflowMixin(ChapterEditorMixin):
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

    def _resolve_hf_archive_repo(self):
        configured = os.getenv("HF_ARCHIVE_REPO", "").strip()
        if configured:
            return configured
        repo, token = self._github_settings()
        response = requests.get(
            f"https://api.github.com/repos/{repo}/actions/variables/HF_ARCHIVE_REPO",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
            timeout=15,
        )
        return response.json().get("value", "").strip() if response.status_code == 200 else ""

    def open_manual_cover_dialog(self):
        if not self.catalog_data:
            messagebox.showinfo("手動封面", "請先解析小說目錄。")
            return
        book_title = self.catalog_data.get("book_title", "")
        catalog_url = self.url_entry.get().strip()
        profile_id = book_profile_id(catalog_url)
        project_root = PROJECT_ROOT
        local_cover = cache_path(project_root, profile_id)
        info_file = local_cover.parent / "cover_information.json"

        top = tk.Toplevel(self.root)
        top.title(f"手動封面｜{book_title}")
        top.geometry("850x650")
        tabs = ttk.Notebook(top)
        tabs.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        info_page, upload_page = ttk.Frame(tabs, padding=10), ttk.Frame(tabs, padding=10)
        tabs.add(info_page, text="封面資訊")
        tabs.add(upload_page, text="上傳封面圖片")

        status = tk.StringVar(value="不會自動呼叫 Gemini；請按下按鈕後才會產生。")
        ttk.Label(info_page, textvariable=status).pack(anchor=tk.W, pady=(0, 8))
        info_tabs = ttk.Notebook(info_page)
        info_tabs.pack(fill=tk.BOTH, expand=True)
        analysis_text = scrolledtext.ScrolledText(info_tabs, wrap=tk.WORD)
        prompt_text = scrolledtext.ScrolledText(info_tabs, wrap=tk.WORD)
        info_tabs.add(analysis_text, text="小說架構／視覺分析")
        info_tabs.add(prompt_text, text="HF 生圖 Prompt")

        def fill_information(data):
            analysis_text.delete("1.0", tk.END)
            analysis_text.insert("1.0", self._format_cover_analysis(data))
            prompt_text.delete("1.0", tk.END)
            prompt_text.insert("1.0", data.get("prompt", ""))
            status.set("已讀取封面資訊；複製與切換頁面不會呼叫 Gemini。")

        def valid_cached_information(data):
            return self._valid_cover_information(data)

        if info_file.is_file():
            try:
                cached = json.loads(info_file.read_text(encoding="utf-8"))
                if valid_cached_information(cached):
                    fill_information(cached)
                else:
                    status.set("舊封面資訊未通過品質檢查，請重新產生。")
            except (OSError, ValueError):
                pass

        def generate_info():
            if (analysis_text.get("1.0", tk.END).strip() and
                    not messagebox.askyesno("重新產生", "這會再次使用 Gemini 額度，確定繼續？", parent=top)):
                return
            generate_button.config(state=tk.DISABLED)
            analysis_text.delete("1.0", tk.END)
            prompt_text.delete("1.0", tk.END)
            status.set("正在取得簡介並呼叫 Gemini…")
            def worker():
                try:
                    data, _ = self._load_or_generate_cover_information(book_title, catalog_url, use_cache=False)
                    self.root.after(0, lambda: (fill_information(data), generate_button.config(state=tk.NORMAL)))
                except Exception as error:
                    self.root.after(0, lambda detail=str(error): (
                        generate_button.config(state=tk.NORMAL), status.set("產生失敗"),
                        messagebox.showerror("封面資訊", detail, parent=top),
                    ))
            threading.Thread(target=worker, daemon=True).start()

        actions = ttk.Frame(info_page)
        actions.pack(fill=tk.X, pady=(8, 0))
        generate_button = ttk.Button(actions, text="開始產生封面資訊", command=generate_info)
        generate_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="複製目前頁面", command=lambda: (
            top.clipboard_clear(), top.clipboard_append(
                analysis_text.get("1.0", tk.END).strip() if info_tabs.index("current") == 0
                else prompt_text.get("1.0", tk.END).strip()
            )
        )).pack(side=tk.LEFT, padx=8)

        upload_status = tk.StringVar(value="選擇或拖曳 JPG／PNG／WEBP；將中央裁切並標準化為 1280×720 JPEG。")
        upload_hint = ttk.Label(upload_page, textvariable=upload_status, wraplength=780)
        upload_hint.pack(anchor=tk.W, pady=8)
        preview = ttk.Label(upload_page, anchor=tk.CENTER)
        preview.pack(fill=tk.BOTH, expand=True, pady=10)

        def select_file():
            path = filedialog.askopenfilename(parent=top, filetypes=[
                ("圖片", "*.jpg *.jpeg *.png *.webp"), ("所有檔案", "*.*")
            ])
            if path:
                process_file(path)

        def process_file(path):
            upload_status.set("正在檢查、裁切、壓縮並同步封面…")
            def worker():
                try:
                    synced_cover, synced_profile_id, repo, details, cloud = self._upload_manual_cover(
                        book_title, catalog_url, path,
                    )
                    def done():
                        with Image.open(synced_cover) as image:
                            shown = image.copy(); shown.thumbnail((720, 405))
                        preview.image = ImageTk.PhotoImage(shown)
                        preview.config(image=preview.image)
                        upload_status.set(
                            f"✓ 上傳成功｜1280×720 JPEG｜{details['bytes']/1024:.0f} KB｜"
                            f"指紋 {synced_profile_id}｜GitHub: {repo}@{cloud['branch']}/{cloud['remote_path']}"
                        )
                        self.log(f"✓ 《{book_title}》手動封面已同步；正式流程將跳過 Gemini 與 HF 生圖。")
                    self.root.after(0, done)
                except Exception as error:
                    self.root.after(0, lambda detail=str(error): (
                        upload_status.set("上傳失敗"), messagebox.showerror("手動封面", detail, parent=top)
                    ))
            threading.Thread(target=worker, daemon=True).start()

        choose_file_button = ttk.Button(upload_page, text="選擇圖片檔案", command=select_file)
        choose_file_button.pack(pady=8)

        def accept_drop(event):
            paths = top.tk.splitlist(event.data)
            if paths:
                process_file(paths[0])
            return "break"

        # Tk 的拖放事件不会从子元件冒泡。预览区覆盖了页面的大部分面积，
        # 因此每一个可见的上传元件都必须各自注册为放置目标。
        drop_widgets = (upload_page, upload_hint, preview, choose_file_button)
        if not self._register_file_drop(drop_widgets, accept_drop):
            upload_status.set("目前環境未載入拖放元件，請使用「選擇圖片檔案」。")
        if local_cover.is_file():
            try:
                with Image.open(local_cover) as image:
                    shown = image.copy(); shown.thumbnail((720, 405))
                preview.image = ImageTk.PhotoImage(shown)
                preview.config(image=preview.image)
                record = self.current_book_profile.get("manual_cover") or {}
                upload_status.set(f"已讀取這本書的手動封面｜指紋 {profile_id}｜{record.get('bytes', local_cover.stat().st_size)/1024:.0f} KB")
            except OSError:
                pass
        elif self.current_book_profile.get("manual_cover"):
            upload_status.set("已找到雲端手動封面紀錄，正在下載並驗證…")
            def restore_worker():
                try:
                    _, _, github_token = self._profile_store()
                    restore_cover(self.current_book_profile["manual_cover"], local_cover, github_token)
                    def restored():
                        with Image.open(local_cover) as image:
                            shown = image.copy(); shown.thumbnail((720, 405))
                        preview.image = ImageTk.PhotoImage(shown)
                        preview.config(image=preview.image)
                        upload_status.set(f"✓ 已從 GitHub 雲端讀取手動封面｜指紋 {profile_id}")
                    self.root.after(0, restored)
                except Exception as error:
                    self.root.after(0, lambda detail=str(error): upload_status.set(f"雲端封面讀取失敗：{detail}"))
            threading.Thread(target=restore_worker, daemon=True).start()
