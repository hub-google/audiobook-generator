from .runtime import *  # noqa: F401,F403


class WorkflowMixin:
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
        project_root = os.path.dirname(os.path.abspath(__file__))
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
                canonical_url = res.get("catalog_url", url)
                profiles, _ = self._profile_store()[0].load()
                _, profile = get_book_profile(profiles, canonical_url, res.get("book_title") or "")
                apply_chapter_title_overrides(
                    res, profile.get("chapter_title_overrides") or {},
                    profile.get("chapter_normalized_number_overrides") or {},
                )
                self.root.after(0, lambda: self._on_parse_success(res, profile))
            except Exception as e:
                error_message = str(e)
                self.root.after(0, lambda message=error_message: self._on_parse_failed(message))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_parse_success(self, res, profile=None):
        self.btn_parse.config(state=tk.NORMAL)
        if res and res.get("success"):
            if res.get("catalog_url") and self.url_entry.get().strip() != res["catalog_url"]:
                self.url_entry.delete(0, tk.END)
                self.url_entry.insert(0, res["catalog_url"])
            self.catalog_data = res
            profile = profile or {}
            self.current_book_profile = dict(profile)
            self.chapter_title_overrides = dict(profile.get("chapter_title_overrides") or {})
            self.chapter_normalized_number_overrides = dict(
                profile.get("chapter_normalized_number_overrides") or {}
            )
            self.cleaner_remove_patterns = list(profile.get("cleaner_remove_patterns") or [])
            self.duplicate_detection = dict(profile.get("duplicate_detection") or {
                "use_normalized_number": True, "use_chapter_name": True, "use_number_and_name": False,
            })
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
            self.renumber_selected_chapters = True
            self.chapter_order = list(range(1, total + 1))
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
        duplicate_use_number = tk.BooleanVar(value=self.duplicate_detection.get("use_normalized_number", True))
        duplicate_use_name = tk.BooleanVar(value=self.duplicate_detection.get("use_chapter_name", True))
        duplicate_use_number_and_name = tk.BooleanVar(value=self.duplicate_detection.get("use_number_and_name", False))
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
        top.geometry("1280x680")
        top.minsize(1050, 480)
        top.resizable(True, True)
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
        show_duplicates_only_var = tk.BooleanVar(value=False)
        
        ttk.Label(control_frame, text="🔍 搜尋:").pack(side=tk.LEFT, padx=(0, 4))
        search_entry = ttk.Entry(control_frame, width=16)
        search_entry.pack(side=tk.LEFT, padx=(0, 10))

        # 中間章節表格 + Scrollbar 區塊
        container = ttk.Frame(top)
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        columns = ("uuid", "output_number", "normalized_number", "display_number", "chapter_name", "duplicate")
        chapter_tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="extended")
        headings = {
            "uuid": "UUID",
            "output_number": "編號章節數",
            "normalized_number": "網站章節數正規化",
            "display_number": "網站顯示章節數",
            "chapter_name": "章節名稱",
            "duplicate": "重複標記",
        }
        widths = {
            "uuid": 65, "output_number": 105, "normalized_number": 155, "display_number": 145,
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

        saved_order = [int(value) for value in self.chapter_order if 1 <= int(value) <= total_chapters]
        chapter_order = []
        seen_order = set()
        for value in saved_order + list(range(1, total_chapters + 1)):
            if value not in seen_order:
                chapter_order.append(value)
                seen_order.add(value)

        chapter_parts = [
            split_chapter_title(title, self.chapter_normalized_number_overrides.get(str(index)))
            for index, title in enumerate(titles, 1)
        ]
        duplicate_analysis = {"duplicate_indices": [], "duplicate_chapters": []}
        duplicate_group_indices = set()

        # 紀錄目前表格顯示的章節編號列表
        visible_indices = []

        def _refresh_duplicates():
            nonlocal duplicate_indices, duplicate_group_indices, duplicate_analysis, chapter_parts
            chapter_parts = [
                split_chapter_title(title, self.chapter_normalized_number_overrides.get(str(index)))
                for index, title in enumerate(titles, 1)
            ]
            duplicate_analysis = analyze_duplicate_chapters(
                titles,
                self.catalog_data.get("chapters", []),
                use_normalized_number=duplicate_use_number.get(),
                use_chapter_name=duplicate_use_name.get(),
                normalized_number_overrides=self.chapter_normalized_number_overrides,
                use_number_and_name=duplicate_use_number_and_name.get(),
            )
            duplicate_indices = {int(value) for value in duplicate_analysis["duplicate_indices"]}
            duplicate_group_indices = set(duplicate_indices)
            for item in duplicate_analysis.get("duplicate_chapters") or []:
                duplicate_group_indices.update(
                    int(value) for value in (item.get("original_indices") or [])
                )
            self.catalog_data.update(duplicate_analysis)

        def _output_numbers():
            selected = [
                idx for idx in chapter_order
                if cur_start <= idx <= cur_end
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

            output_numbers = _output_numbers()
            for global_idx in chapter_order:
                if not s_idx <= global_idx <= e_idx:
                    continue
                if show_duplicates_only_var.get() and global_idx not in duplicate_group_indices:
                    continue
                parts = chapter_parts[global_idx - 1]
                is_checked = chapter_state.get(global_idx, True)
                output_number = ""
                if is_checked and cur_start <= global_idx <= cur_end:
                    output_number = output_numbers.get(global_idx, "")
                values = (
                    global_idx,
                    f"{'☑' if is_checked else '☐'} {output_number}".rstrip(),
                    parts["normalized_number"],
                    parts["display_number"],
                    parts["chapter_name"],
                    "重複" if global_idx in duplicate_group_indices else "",
                )
                display_text = " ".join(str(value) for value in values)

                if filter_text and filter_text not in display_text.lower():
                    continue

                visible_indices.append(global_idx)
                chapter_tree.insert("", tk.END, iid=str(global_idx), values=values)

        chk_show_all = ttk.Checkbutton(control_frame, text="🌐 顯示全書章節", variable=show_all_var, command=_update_listbox)
        chk_show_all.pack(side=tk.LEFT, padx=(0, 10))

        duplicate_filter_button = ttk.Button(control_frame, text="只看重複章節")
        duplicate_filter_button.pack(side=tk.LEFT, padx=(0, 10))

        catalog_url = self.url_entry.get().strip()

        def _open_catalog_page():
            if not catalog_url:
                messagebox.showwarning("提示", "目前沒有可開啟的目錄網址！", parent=top)
                return
            try:
                if not webbrowser.open(catalog_url):
                    raise RuntimeError("系統未能啟動預設瀏覽器")
            except Exception as error:
                messagebox.showerror(
                    "開啟目錄網頁失敗",
                    f"無法使用預設瀏覽器開啟目錄網址：\n{catalog_url}\n\n{error}",
                    parent=top,
                )

        ttk.Button(
            control_frame,
            text="查看目錄網頁",
            command=_open_catalog_page,
        ).pack(side=tk.LEFT, padx=(0, 10))

        def _toggle_duplicate_filter():
            show_duplicates_only_var.set(not show_duplicates_only_var.get())
            duplicate_filter_button.config(
                text="看全部章節" if show_duplicates_only_var.get() else "只看重複章節"
            )
            _update_listbox()
            if show_duplicates_only_var.get() and not visible_indices:
                info_lbl.config(text="目前沒有符合範圍與搜尋條件的重複章節")

        duplicate_filter_button.config(command=_toggle_duplicate_filter)

        search_entry.bind("<KeyRelease>", lambda e: _update_listbox())

        def _show_duplicate_details(index):
            matches = find_direct_duplicate_matches(
                titles, index,
                use_normalized_number=duplicate_use_number.get(),
                use_chapter_name=duplicate_use_name.get(),
                normalized_number_overrides=self.chapter_normalized_number_overrides,
                use_number_and_name=duplicate_use_number_and_name.get(),
            )
            if not matches:
                return
            dialog = tk.Toplevel(top)
            dialog.title(f"重複章節明細｜UUID {index}")
            dialog.geometry("1050x340")
            dialog.transient(top)
            body = ttk.Frame(dialog, padding=10)
            body.pack(fill=tk.BOTH, expand=True)
            reason_labels = {
                "normalized_chapter_number": "網站章節數正規化",
                "chapter_name_without_whitespace": "章節名稱（去除空白）",
                "normalized_chapter_number_and_name_without_whitespace": "網站章節數正規化 & 章節名稱（去除空白）",
            }
            ttk.Label(
                body, text=f"UUID {index} 的直接符合章節：{len(matches)} 筆（不串聯其他章節的關係）",
            ).pack(anchor=tk.W, pady=(0, 8))
            detail_columns = ("uuid", "normalized", "display", "name", "reason")
            detail = ttk.Treeview(body, columns=detail_columns, show="headings")
            for key, label, width in (
                ("uuid", "UUID", 70), ("normalized", "網站章節數正規化", 160),
                ("display", "網站顯示章節數", 160), ("name", "章節名稱", 300),
                ("reason", "與目前章節直接符合的條件", 310),
            ):
                detail.heading(key, text=label)
                detail.column(key, width=width, anchor=tk.W if key in {"name", "reason"} else tk.CENTER)
            selected_parts = chapter_parts[index - 1]
            detail.insert("", tk.END, values=(
                index, selected_parts["normalized_number"], selected_parts["display_number"],
                selected_parts["chapter_name"], "目前章節",
            ))
            for match in matches:
                uuid_value = int(match["index"])
                parts = chapter_parts[uuid_value - 1]
                reasons = "、".join(reason_labels.get(value, value) for value in match["reasons"])
                detail.insert("", tk.END, values=(
                    uuid_value, parts["normalized_number"], parts["display_number"],
                    parts["chapter_name"], reasons,
                ))
            detail.pack(fill=tk.BOTH, expand=True)
            ttk.Button(body, text="關閉", command=dialog.destroy).pack(anchor=tk.E, pady=(8, 0))

        def _toggle_item(event=None):
            if event is not None and getattr(event, "num", None) == 1:
                if chapter_tree.identify_region(event.x, event.y) != "cell":
                    return
                column = chapter_tree.identify_column(event.x)
                row = chapter_tree.identify_row(event.y)
                if not row:
                    return
                if column == "#6":
                    if int(row) in duplicate_group_indices:
                        _show_duplicate_details(int(row))
                    return
                if column == "#5":
                    return
                # Mouse clicks only toggle the checkbox column.  Other columns
                # remain available for Ctrl/Shift multi-selection and dragging.
                if column != "#2":
                    return
            sel = chapter_tree.selection()
            if not sel:
                return
            selected_indices = [int(value) for value in sel if int(value) in visible_indices]
            if selected_indices:
                new_value = not chapter_state[selected_indices[0]]
                for g_idx in selected_indices:
                    chapter_state[g_idx] = new_value
                # 勾選變更可能讓後面所有製作章號前移或後移。
                _update_listbox()
                existing = [str(value) for value in selected_indices if chapter_tree.exists(str(value))]
                if existing:
                    chapter_tree.selection_set(existing)
                    chapter_tree.focus(existing[0])

        # 單擊選取或按空白鍵切換狀態
        chapter_tree.bind("<ButtonRelease-1>", _toggle_item)
        chapter_tree.bind("<space>", lambda e: (_toggle_item(), "break"))

        def _edit_chapter_name(event):
            if chapter_tree.identify_region(event.x, event.y) != "cell" or chapter_tree.identify_column(event.x) != "#5":
                return
            row = chapter_tree.identify_row(event.y)
            if not row:
                return
            index = int(row)
            x, y, width, height = chapter_tree.bbox(row, "chapter_name")
            editor = ttk.Entry(chapter_tree)
            editor.insert(0, chapter_parts[index - 1]["chapter_name"])
            editor.place(x=x, y=y, width=width, height=height)
            editor.focus_set()
            editor.select_range(0, tk.END)
            closed = False

            def finish(save=True):
                nonlocal closed
                if closed:
                    return
                closed = True
                if save:
                    new_name = editor.get().strip()
                    if not new_name:
                        messagebox.showwarning("章節名稱", "章節名稱不能是空白。", parent=top)
                        closed = False
                        editor.focus_set()
                        return
                    parts = chapter_parts[index - 1]
                    full_title = " ".join(value for value in (parts["display_number"], new_name) if value).strip()
                    titles[index - 1] = full_title
                    self.catalog_data["chapter_titles"][index - 1] = full_title
                    self.chapter_title_overrides[str(index)] = full_title
                    _refresh_duplicates()
                    _update_listbox()
                editor.destroy()

            editor.bind("<Return>", lambda _event: finish(True))
            editor.bind("<Escape>", lambda _event: finish(False))
            editor.bind("<FocusOut>", lambda _event: finish(True))

        def _edit_normalized_number(event):
            if (chapter_tree.identify_region(event.x, event.y) != "cell" or
                    chapter_tree.identify_column(event.x) != "#3"):
                return
            row = chapter_tree.identify_row(event.y)
            if not row:
                return
            index = int(row)
            x, y, width, height = chapter_tree.bbox(row, "normalized_number")
            editor = ttk.Entry(chapter_tree)
            editor.insert(0, chapter_parts[index - 1]["normalized_number"])
            editor.place(x=x, y=y, width=width, height=height)
            editor.focus_set()
            editor.select_range(0, tk.END)
            closed = False

            def finish(save=True):
                nonlocal closed
                if closed:
                    return
                closed = True
                if save:
                    value = editor.get().strip()
                    if value:
                        try:
                            normalized_value = normalize_positive_chapter_number(value)
                        except ValueError:
                            messagebox.showwarning(
                                "網站章節數正規化", "請輸入大於 0 的整數或小數；留白可恢復自動解析。", parent=top,
                            )
                            closed = False
                            editor.focus_set()
                            return
                        self.chapter_normalized_number_overrides[str(index)] = normalized_value
                    else:
                        self.chapter_normalized_number_overrides.pop(str(index), None)
                    self.catalog_data["chapter_normalized_number_overrides"] = dict(
                        self.chapter_normalized_number_overrides
                    )
                    _refresh_duplicates()
                    _update_listbox()
                    info_lbl.config(text=f"UUID {index} 的網站章節數正規化已更新；按排序按鈕才會移動章節")
                editor.destroy()

            editor.bind("<Return>", lambda _event: finish(True))
            editor.bind("<Escape>", lambda _event: finish(False))
            editor.bind("<FocusOut>", lambda _event: finish(True))

        def _edit_chapter_cell(event):
            if chapter_tree.identify_column(event.x) == "#3":
                _edit_normalized_number(event)
            elif chapter_tree.identify_column(event.x) == "#5":
                _edit_chapter_name(event)

        chapter_tree.bind("<Double-1>", _edit_chapter_cell)

        drag_state = {"active": False, "items": []}

        def _drag_start(event):
            if chapter_tree.identify_region(event.x, event.y) != "cell":
                return
            row = chapter_tree.identify_row(event.y)
            if not row or chapter_tree.identify_column(event.x) != "#1":
                return
            if row not in chapter_tree.selection():
                chapter_tree.selection_set(row)
            selected = {int(value) for value in chapter_tree.selection()}
            drag_state["items"] = [value for value in chapter_order if value in selected]
            drag_state["active"] = bool(drag_state["items"])

        def _drag_motion(event):
            if drag_state["active"]:
                row = chapter_tree.identify_row(event.y)
                if row:
                    chapter_tree.selection_set([str(value) for value in drag_state["items"]])
                    chapter_tree.focus(row)
            return "break"

        def _drag_end(event):
            if not drag_state["active"]:
                return
            moving = list(drag_state["items"])
            drag_state["active"] = False
            target_row = chapter_tree.identify_row(event.y)
            if not target_row or int(target_row) in moving:
                return
            remaining = [value for value in chapter_order if value not in set(moving)]
            target = int(target_row)
            insert_at = remaining.index(target)
            chapter_order[:] = remaining[:insert_at] + moving + remaining[insert_at:]
            self.renumber_selected_chapters = True
            _update_listbox()
            existing = [str(value) for value in moving if chapter_tree.exists(str(value))]
            if existing:
                chapter_tree.selection_set(existing)
                chapter_tree.see(existing[0])
            info_lbl.config(text=f"已移動 {len(moving)} 章｜編號章節數已依目前順序重算")

        chapter_tree.bind("<ButtonPress-1>", _drag_start, add="+")
        chapter_tree.bind("<B1-Motion>", _drag_motion, add="+")
        chapter_tree.bind("<ButtonRelease-1>", _drag_end, add="+")

        _refresh_duplicates()
        _update_listbox()

        # 底部按鈕區
        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X, padx=15, pady=(4, 15))
        order_frame = ttk.Frame(top)
        order_frame.pack(fill=tk.X, padx=15, pady=(8, 2), before=btn_frame)

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
            pending_use_number_and_name = tk.BooleanVar(value=duplicate_use_number_and_name.get())
            ttk.Label(body, text="勾選任一符合即視為重複的條件：").pack(anchor=tk.W, pady=(0, 8))
            ttk.Checkbutton(
                body, text="網站章節數正規化", variable=pending_use_number,
            ).pack(anchor=tk.W, pady=3)
            ttk.Checkbutton(
                body, text="章節名稱（去除空白）", variable=pending_use_name,
            ).pack(anchor=tk.W, pady=3)
            ttk.Checkbutton(
                body, text="網站章節數正規化 & 章節名稱（去除空白）",
                variable=pending_use_number_and_name,
            ).pack(anchor=tk.W, pady=3)

            def _apply_conditions():
                if not (pending_use_number.get() or pending_use_name.get() or pending_use_number_and_name.get()):
                    messagebox.showwarning("重複判斷條件", "請至少勾選一個判斷條件。", parent=dialog)
                    return
                duplicate_use_number.set(pending_use_number.get())
                duplicate_use_name.set(pending_use_name.get())
                duplicate_use_number_and_name.set(pending_use_number_and_name.get())
                self.duplicate_detection = {
                    "use_normalized_number": duplicate_use_number.get(),
                    "use_chapter_name": duplicate_use_name.get(),
                    "use_number_and_name": duplicate_use_number_and_name.get(),
                }
                _refresh_duplicates()
                _update_listbox()
                info_lbl.config(text=f"已重新判斷重複章節｜共標記 {len(duplicate_indices)} 個")
                dialog.destroy()

                task = self._selected_task()
                if task:
                    def save_conditions():
                        try:
                            store, _, _ = self._profile_store()
                            store.mutate(
                                lambda data: update_book_profile(
                                    data, task.get("catalog_url") or "", task.get("book_title") or "",
                                    cleaner_remove_patterns=self.cleaner_remove_patterns,
                                    duplicate_detection=self.duplicate_detection,
                                    chapter_title_overrides=self.chapter_title_overrides,
                                    chapter_normalized_number_overrides=self.chapter_normalized_number_overrides,
                                ),
                                f"Update duplicate detection for {task.get('book_title') or 'audiobook'}",
                            )
                        except Exception as error:
                            self.root.after(0, lambda detail=str(error): messagebox.showerror(
                                "儲存重複判斷條件失敗", detail, parent=top,
                            ))
                    threading.Thread(target=save_conditions, daemon=True).start()

            ttk.Button(body, text="套用", command=_apply_conditions).pack(anchor=tk.E, pady=(12, 0))

        def _renumber_selected():
            self.renumber_selected_chapters = True
            _update_listbox()
            info_lbl.config(
                text="已依勾選順序重新編號｜網站索引仍保留供下載定位"
            )

        def _selected_in_order():
            selected = {int(value) for value in chapter_tree.selection()}
            return [value for value in chapter_order if value in selected]

        def _move_selected(delta):
            selected = set(_selected_in_order())
            if not selected:
                info_lbl.config(text="請先選取要移動的章節（可用 Ctrl／Shift 多選）")
                return
            chapter_order[:] = move_chapter_order(chapter_order, selected, delta)
            self.renumber_selected_chapters = True
            _update_listbox()
            existing = [str(value) for value in chapter_order if value in selected and chapter_tree.exists(str(value))]
            if existing:
                chapter_tree.selection_set(existing)
                chapter_tree.see(existing[0])
            info_lbl.config(text=f"已{'上' if delta < 0 else '下'}移 {len(selected)} 章｜編號章節數已重算")

        def _sort_by_normalized_number():
            original_positions = {value: index for index, value in enumerate(chapter_order)}

            def sort_key(source_index):
                normalized = chapter_parts[source_index - 1]["normalized_number"]
                try:
                    return (0, Decimal(str(normalized)), original_positions[source_index])
                except (InvalidOperation, ValueError):
                    pass
                return (1, 0, original_positions[source_index])

            chapter_order.sort(key=sort_key)
            self.renumber_selected_chapters = True
            _update_listbox()
            info_lbl.config(text="已依網站章節正規化順序排列｜編號章節數即為實際製作順序")

        def _restore_source_order():
            chapter_order[:] = list(range(1, total_chapters + 1))
            self.renumber_selected_chapters = True
            _update_listbox()
            info_lbl.config(text="已還原網站原始 UUID 順序｜編號章節數已重算")

        def _close_dialog():
            self.excluded_chapters.clear()
            for g_idx, is_checked in chapter_state.items():
                if not is_checked:
                    self.excluded_chapters.add(g_idx)
            self.chapter_order = list(chapter_order)
            # Once an explicit order is saved, output numbering is always the
            # actual consecutive production order shown in this dialog.
            self.renumber_selected_chapters = True
            self._update_chapter_selection_summary(cur_start, cur_end)
            top.destroy()

            # Persist per-book edits independently of whether this is a new or
            # already queued book. GitHubBookProfileStore performs SHA-guarded
            # read/merge/write retries, so cleaner rules and unrelated books are preserved.
            catalog_url = self.url_entry.get().strip()
            book_title = (self.catalog_data or {}).get("book_title", "")
            title_overrides = dict(self.chapter_title_overrides)
            number_overrides = dict(self.chapter_normalized_number_overrides)
            cleaner_patterns = list(self.cleaner_remove_patterns)
            detection = dict(self.duplicate_detection)

            def save_profile():
                try:
                    store, _, _ = self._profile_store()
                    store.mutate(
                        lambda data: update_book_profile(
                            data, catalog_url, book_title,
                            cleaner_remove_patterns=cleaner_patterns,
                            duplicate_detection=detection,
                            chapter_title_overrides=title_overrides,
                            chapter_normalized_number_overrides=number_overrides,
                        ),
                        f"Update chapter overrides for {book_title or 'audiobook'}",
                    )
                    self.root.after(0, lambda: self.log("✓ 每本小說永久章節設定已儲存"))
                except Exception as error:
                    self.root.after(0, lambda detail=str(error): messagebox.showerror(
                        "儲存永久章節設定失敗", detail,
                    ))

            threading.Thread(target=save_profile, daemon=True).start()

        top.protocol("WM_DELETE_WINDOW", _close_dialog)

        ttk.Button(btn_frame, text="全選", command=_select_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="全不選", command=_deselect_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="反選", command=_invert_select).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="重複判斷條件", command=_choose_duplicate_conditions).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="排除重複章節", command=_exclude_duplicates).pack(side=tk.LEFT, padx=4)
        ttk.Label(order_frame, text="實際製作順序：").pack(side=tk.LEFT, padx=(4, 2))
        ttk.Button(order_frame, text="依網站章節正規化順序排列", command=_sort_by_normalized_number).pack(side=tk.LEFT, padx=4)
        ttk.Button(order_frame, text="上移 ▲", command=lambda: _move_selected(-1)).pack(side=tk.LEFT, padx=4)
        ttk.Button(order_frame, text="下移 ▼", command=lambda: _move_selected(1)).pack(side=tk.LEFT, padx=4)
        ttk.Button(order_frame, text="還原原始順序", command=_restore_source_order).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="確定", style="Accent.TButton", command=_close_dialog).pack(side=tk.RIGHT, padx=5)

        def _move_up_shortcut(_event):
            _move_selected(-1)
            return "break"

        def _move_down_shortcut(_event):
            _move_selected(1)
            return "break"

        chapter_tree.bind("<Alt-Up>", _move_up_shortcut)
        chapter_tree.bind("<Alt-Down>", _move_down_shortcut)

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
                start_int = int(start_chap)
                end_int = int(end_chap)
                chapter_label = format_chapter_label(
                    start_int, end_int,
                    excluded_chapters=self.excluded_chapters,
                    renumber_selected=self.renumber_selected_chapters,
                )
                dispatch_url = f"https://api.github.com/repos/{repo}/actions/workflows/audiobook.yml/dispatches"
                payload = {
                    "ref": "master",
                    "inputs": {
                        "book_title": self.catalog_data.get("book_title", "待解析書名"),
                        "chapter_label": chapter_label,
                        "catalog_url": url,
                        "start_chap": start_chap,
                        "end_chap": end_chap,
                        "exclude_chapters": ",".join(map(str, sorted(list(self.excluded_chapters)))) if hasattr(self, 'excluded_chapters') and self.excluded_chapters else "",
                        "renumber_selected": "true" if self.renumber_selected_chapters else "false",
                        "chapter_title_overrides_b64": base64.b64encode(
                            json.dumps(self.chapter_title_overrides, ensure_ascii=False).encode("utf-8")
                        ).decode("ascii"),
                        "chapter_order_b64": base64.b64encode(
                            json.dumps(self.chapter_order).encode("utf-8")
                        ).decode("ascii"),
                        "book_profile_snapshot_b64": base64.b64encode(
                            json.dumps(profile_snapshot(
                                book_profile_id(url), {
                                    "profile_revision": 0,
                                    "catalog_url": url,
                                    "catalog_identity": self.catalog_data.get("catalog_identity", ""),
                                    "cleaner_remove_patterns": self.cleaner_remove_patterns,
                                    "duplicate_detection": self.duplicate_detection,
                                    "chapter_title_overrides": self.chapter_title_overrides,
                                    "chapter_normalized_number_overrides": self.chapter_normalized_number_overrides,
                                    "manual_cover": dict(self.current_book_profile.get("manual_cover") or {}),
                                }
                            ), ensure_ascii=False).encode("utf-8")
                        ).decode("ascii"),
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
                cancel_url = f"https://api.github.com/repos/{self.current_repo}/actions/runs/{self.current_run_id}/force-cancel"
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
