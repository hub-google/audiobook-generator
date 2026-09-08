from .runtime import *  # noqa: F401,F403


class ReviewMixin:
    def _selected_task(self):
        tasks = self._selected_tasks()
        return tasks[0] if tasks else None

    def _selected_tasks(self):
        selected_ids = set(self.queue_tree.selection())
        if not selected_ids or not self.cloud_queue:
            return []
        return [
            task for task in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", [])
            if task.get("task_id") in selected_ids
        ]

    @staticmethod
    def _valid_cover_information(data):
        return (
            isinstance(data, dict) and data.get("status") == "ok"
            and len(data.get("story_facts") or []) >= 5
            and bool(data.get("analysis")) and bool(data.get("prompt"))
        )

    @staticmethod
    def _format_cover_analysis(data):
        analysis = data.get("analysis") or {}
        research = data.get("research") or {}
        mode_text = {
            "web_evidence": "聯網資料",
            "hybrid_web_and_model_knowledge": "聯網資料＋Gemini 內建知識（已二次審核）",
            "internal_knowledge_fallback": "Gemini 內建知識備援（已二次審核）",
        }.get(research.get("mode"), "未知資料模式")
        sources = "\n".join(
            f"• [{item.get('id')}] {item.get('title')}｜{item.get('url')}"
            for item in research.get("sources") or []
        )
        facts = "\n".join(
            f"• {item.get('fact')}（{', '.join(item.get('source_ids') or [])}）"
            for item in data.get("story_facts") or []
        )
        sections = [
            f"【資料模式】\n{mode_text}", f"【資料來源】\n{sources}", f"【Gemini 故事事實】\n{facts}",
        ]
        sections.extend(f"【{key}】\n{value}" for key, value in analysis.items())
        return "\n\n".join(sections)

    def _download_artifact_files(self, run_id, name_prefix, progress=None):
        repo, token = self._github_settings()
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if progress:
            progress("正在查詢 GitHub artifact…")
        response = requests.get(f"https://api.github.com/repos/{repo}/actions/runs/{int(run_id)}/artifacts",
                                headers=headers, params={"per_page": 100}, timeout=30)
        response.raise_for_status()
        artifacts = [item for item in response.json().get("artifacts", [])
                     if not item.get("expired") and str(item.get("name") or "").startswith(name_prefix)]
        if not artifacts:
            raise RuntimeError(f"Run {run_id} 找不到 {name_prefix} artifact，或 artifact 已過期。")
        files = {}
        for index, artifact in enumerate(artifacts, 1):
            if progress:
                progress(f"正在下載 artifact {index}/{len(artifacts)}：{artifact.get('name') or artifact.get('id')}")
            bundle = requests.get(artifact["archive_download_url"], headers=headers, timeout=90)
            bundle.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
                for member in archive.infolist():
                    if not member.is_dir():
                        files[member.filename.replace("\\", "/")] = archive.read(member)
        return files

    def open_ad_analysis_results(self):
        task = self._selected_task()
        if not task or not task.get("scrape_run_id"):
            messagebox.showinfo("廣告分析", "TXT 抓取完成後才能查看。")
            return
        top = tk.Toplevel(self.root); top.title(f"文字清理／關鍵字審核｜《{task.get('book_title')}》"); top.geometry("1480x860"); top.minsize(1050, 650)
        notebook = ttk.Notebook(top); notebook.pack(fill=tk.BOTH, expand=True)
        analysis_page = ttk.Frame(notebook); sample_page = ttk.Frame(notebook)
        notebook.add(analysis_page, text="候選詞句與排除設定")
        notebook.add(sample_page, text="Raw／Clean 文字抽查")
        status = tk.StringVar(value="正在下載廣告分析結果…")
        status_bar = ttk.Frame(analysis_page, padding=8); status_bar.pack(fill=tk.X)
        ttk.Label(status_bar, textvariable=status).pack(side=tk.LEFT, fill=tk.X, expand=True)
        retry_button = ttk.Button(status_bar, text="重新載入", state=tk.DISABLED)
        retry_button.pack(side=tk.RIGHT)

        filter_bar = ttk.LabelFrame(analysis_page, text="篩選與批次編輯", padding=6); filter_bar.pack(fill=tk.X, padx=8)
        search_text = tk.StringVar(); decision_filter = tk.StringVar(value="全部狀態")
        ttk.Label(filter_bar, text="搜尋：").pack(side=tk.LEFT)
        search_entry = ttk.Entry(filter_bar, textvariable=search_text, width=28); search_entry.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(filter_bar, text="處理狀態：").pack(side=tk.LEFT)
        decision_combo = ttk.Combobox(filter_bar, textvariable=decision_filter,
                                      values=("全部狀態", "僅顯示待去除", "僅顯示已保留"),
                                      state="readonly", width=14)
        decision_combo.pack(side=tk.LEFT, padx=(0, 10))
        count_label = ttk.Label(filter_bar, text="顯示：0 項｜去除：0 項｜保留：0 項")
        count_label.pack(side=tk.RIGHT)

        panes = ttk.Panedwindow(analysis_page, orient=tk.HORIZONTAL); panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        tree_frame = ttk.Frame(panes)
        tree = ttk.Treeview(tree_frame, columns=("decision", "kind", "text", "count", "chapters", "score", "reason"),
                            show="headings", selectmode="extended")
        for key, title, width in (("decision", "處理狀態", 85), ("kind", "類型", 105), ("text", "疑似廣告詞句／重複行", 390),
                                  ("count", "次數", 55), ("chapters", "影響章節", 130), ("score", "可疑度", 65), ("reason", "判定理由", 190)):
            tree.heading(key, text=title); tree.column(key, width=width)
        tree.column("text", anchor=tk.W); tree.column("reason", anchor=tk.W)
        tree.tag_configure("remove", foreground="#9b0000"); tree.tag_configure("keep", foreground="#777777")
        tree_y = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree_x = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        tree.grid(row=0, column=0, sticky="nsew"); tree_y.grid(row=0, column=1, sticky="ns"); tree_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1); tree_frame.columnconfigure(0, weight=1)
        detail_frame = ttk.LabelFrame(panes, text="選定詞句的判定與原文上下文", padding=4)
        detail = scrolledtext.ScrolledText(detail_frame, wrap=tk.WORD, font=("Microsoft JhengHei", 10))
        detail.pack(fill=tk.BOTH, expand=True)
        panes.add(tree_frame, weight=3); panes.add(detail_frame, weight=2)
        rows = {}; row_order = []; review_identity = {}; chapter_texts = {}
        sort_state = {"column": None, "reverse": False}
        sample_refresh = {"callback": None}

        def selected_patterns():
            return sorted({item.get("text", "").strip() for item in rows.values()
                           if item.get("approved_remove") and item.get("text", "").strip()}, key=len, reverse=True)

        def refresh_samples():
            if sample_refresh["callback"]:
                sample_refresh["callback"](selected_patterns())

        def row_values(item):
            chapters = item.get("affected_chapters") or []
            return ("☑ 去除" if item.get("approved_remove") else "☐ 保留", item.get("kind") or "", item.get("text") or "",
                    item.get("count", "—"), ",".join(map(str, chapters[:15])) + ("…" if len(chapters) > 15 else ""),
                    item.get("score", "—"), item.get("reason") or "")

        def refresh_tree(*_args):
            selected = set(tree.selection()); tree.delete(*tree.get_children())
            keyword = search_text.get().strip().lower(); choice = decision_filter.get()
            visible = 0
            visible_ids = list(row_order)
            column = sort_state["column"]
            if column:
                visible_ids.sort(
                    key=lambda iid: self._ad_review_sort_value(rows.get(iid) or {}, column),
                    reverse=sort_state["reverse"],
                )
            for iid in visible_ids:
                item = rows.get(iid)
                if not item: continue
                remove = bool(item.get("approved_remove"))
                haystack = " ".join(str(item.get(key) or "") for key in ("text", "kind", "reason")).lower()
                if keyword and keyword not in haystack: continue
                if choice == "僅顯示待去除" and not remove: continue
                if choice == "僅顯示已保留" and remove: continue
                tree.insert("", tk.END, iid=iid, values=row_values(item), tags=("remove" if remove else "keep",))
                visible += 1
                if iid in selected: tree.selection_add(iid)
            remove_count = sum(bool(item.get("approved_remove")) for item in rows.values())
            count_label.config(text=f"顯示：{visible}/{len(rows)} 項｜去除：{remove_count} 項｜保留：{len(rows) - remove_count} 項")

        column_titles = {"decision": "處理狀態", "kind": "類型", "text": "疑似廣告詞句／重複行",
                         "count": "次數", "chapters": "影響章節", "score": "可疑度", "reason": "判定理由"}
        def sort_by(column):
            if sort_state["column"] == column:
                sort_state["reverse"] = not sort_state["reverse"]
            else:
                sort_state.update(column=column, reverse=False)
            for key, title in column_titles.items():
                marker = (" ▼" if sort_state["reverse"] else " ▲") if key == column else ""
                tree.heading(key, text=title + marker, command=lambda value=key: sort_by(value))
            refresh_tree()
        for key, title in column_titles.items():
            tree.heading(key, text=title, command=lambda value=key: sort_by(value))

        search_text.trace_add("write", refresh_tree); decision_combo.bind("<<ComboboxSelected>>", refresh_tree)
        chapter_bar = ttk.Frame(analysis_page, padding=8); chapter_bar.pack(fill=tk.X)
        ttk.Label(chapter_bar, text="查看章節 Raw／Clean：").pack(side=tk.LEFT)
        chapter_choice = ttk.Combobox(chapter_bar, state="readonly", width=15); chapter_choice.pack(side=tk.LEFT)
        def preview_chapter(_event=None):
            raw = chapter_texts.get(chapter_choice.get(), "")
            if not raw: return
            title, _, body = raw.partition("\n")
            patterns = [item["text"] for item in rows.values() if item.get("approved_remove")]
            cleaned = clean_text_content(body, title, task.get("book_title") or "", patterns)
            detail.delete("1.0", tk.END)
            detail.insert("1.0", f"【正規化 Raw】\n{raw}\n\n【套用目前規則後 Clean】\n{chunk_text(cleaned)}")
        chapter_choice.bind("<<ComboboxSelected>>", preview_chapter)
        def select(_event=None):
            if not tree.selection(): return
            item = rows[tree.selection()[0]]; detail.delete("1.0", tk.END)
            detail.insert("1.0", f"判定原因：{item.get('reason')}\n\n影響章節：{item.get('affected_chapters')}\n\n" + "\n\n---\n\n".join(item.get("samples") or []))
        tree.bind("<<TreeviewSelect>>", select)
        def set_selected(value=None):
            for iid in tree.selection():
                item = rows[iid]; item["approved_remove"] = (not item.get("approved_remove", False)) if value is None else value
            refresh_tree(); select(); refresh_samples()
        def toggle(_event=None):
            set_selected()
            return "break"
        def click_status(event):
            if tree.identify("region", event.x, event.y) == "cell" and tree.identify_column(event.x) == "#1":
                iid = tree.identify_row(event.y)
                if iid:
                    tree.selection_set(iid); set_selected()
        def edit_selected():
            selected = tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("編輯廣告詞句", "請先選取一筆候選。", parent=top); return
            iid = selected[0]; item = rows[iid]; old_text = str(item.get("text") or "")
            value = simpledialog.askstring("編輯廣告詞句", "要比對並刪除的文字：", initialvalue=old_text, parent=top)
            if value is None: return
            patterns = validate_remove_patterns([value])
            if not patterns:
                messagebox.showerror("無法儲存", "詞句不可為空白或無效內容。", parent=top); return
            item["text"] = patterns[0]
            if patterns[0] != old_text:
                item["reason"] = f"人工編輯（原詞句：{old_text}）"
            refresh_tree(); tree.selection_set(iid); tree.see(iid); select(); refresh_samples()
        def remove_selected():
            selected = list(tree.selection())
            for iid in selected:
                rows.pop(iid, None)
                if iid in row_order: row_order.remove(iid)
            refresh_tree(); detail.delete("1.0", tk.END); refresh_samples()
        def double_click(event):
            if tree.identify_column(event.x) == "#3": edit_selected()
            else: toggle()
            return "break"
        tree.bind("<Button-1>", click_status, add="+")
        tree.bind("<space>", toggle); tree.bind("<Delete>", lambda _event: (set_selected(False), "break")[1])
        tree.bind("<Double-1>", double_click)
        manual_text = tk.StringVar()
        ttk.Entry(chapter_bar, textvariable=manual_text, width=35).pack(side=tk.LEFT, padx=8)
        def add_manual():
            patterns = validate_remove_patterns([manual_text.get()])
            if not patterns: return
            iid = f"manual-{time.time_ns()}"
            rows[iid] = {"text": patterns[0], "approved_remove": True, "reason": "人工新增", "samples": []}
            row_order.append(iid); manual_text.set(""); refresh_tree(); tree.selection_set(iid); tree.see(iid); refresh_samples()
        ttk.Button(chapter_bar, text="新增刪除文字", command=add_manual).pack(side=tk.LEFT)
        actions = ttk.Frame(analysis_page, padding=(8, 0, 8, 8)); actions.pack(fill=tk.X)
        ttk.Button(actions, text="☑ 設為去除", command=lambda: set_selected(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="☐ 設為保留", command=lambda: set_selected(False)).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="反選", command=set_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="編輯詞句", command=edit_selected).pack(side=tk.LEFT, padx=(12, 2))
        ttk.Button(actions, text="從名單移除", command=remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Label(actions, text="點狀態欄或 Space 切換；雙擊詞句可編輯；Delete 設為保留。", foreground="#555").pack(side=tk.LEFT, padx=12)
        approve_button = ttk.Button(actions, text="確認文字清理設定", style="Accent.TButton", state=tk.DISABLED)
        approve_button.pack(side=tk.RIGHT)

        def approve():
            patterns = selected_patterns()
            self.cleaner_remove_patterns = list(patterns)
            approve_button.config(state=tk.DISABLED); status.set("正在將審核結果儲存到雲端…")
            def save_worker():
                try:
                    profile_store, _, _ = self._profile_store()
                    profile_store.mutate(
                        lambda data: update_book_profile(data, task.get("catalog_url") or "", task.get("book_title") or "",
                                                         cleaner_remove_patterns=patterns),
                        f"Approve advertisement review for {task['task_id']}",
                    )
                    queue_store, _, _ = self._queue_store()
                    reviewed_at = datetime.now().astimezone().isoformat()
                    queue = queue_store.mutate(
                        lambda data: confirm_preflight_review(data, task["task_id"], task["scrape_run_id"], task["cover_run_id"],
                            {"status": "approved", "approved_at": reviewed_at, **review_identity,
                             "remove_patterns": patterns,
                             "keep_patterns": [item["text"] for item in rows.values() if not item.get("approved_remove")]}),
                        f"Confirm text cleaning review for {task['task_id']}",
                    )
                    self.cloud_queue = queue
                    self.root.after(0, lambda: (self._render_queue(queue), top.destroy(),
                                                self.log(f"✓ 《{task.get('book_title')}》文字清理設定已儲存，等待開始第二階段後製。")))
                except Exception as error:
                    self.root.after(0, lambda detail=str(error): (status.set(f"儲存失敗：{detail}"), approve_button.config(state=tk.NORMAL)))
            threading.Thread(target=save_worker, daemon=True).start()
        approve_button.config(command=approve)
        load_generation = {"value": 0}

        def set_load_status(message, generation):
            if generation == load_generation["value"] and top.winfo_exists():
                status.set(message)

        def worker(generation):
            try:
                progress = lambda message: self.root.after(0, lambda value=message: set_load_status(value, generation))
                files = self._download_artifact_files(
                    task["scrape_run_id"], f"scrape-review-{task['task_id']}", progress=progress,
                )
                reports = [json.loads(data.decode("utf-8")) for name, data in files.items()
                           if name.endswith(".json") and "ad-candidates-" in name]
                candidates = [item for report in reports for item in report.get("candidates", [])]
                if len(reports) != 1 or reports[0].get("task_id") != task["task_id"]:
                    raise RuntimeError("廣告報告任務身分不一致或報告缺失")
                progress("正在載入已儲存的文字清理規則…")
                profiles, _ = self._profile_store()[0].load()
                _, profile = get_book_profile(profiles, task.get("catalog_url") or "", task.get("book_title") or "")
                saved_patterns = set(profile.get("cleaner_remove_patterns") or [])
                known = {item["text"] for item in candidates}
                candidates.extend({"text": text, "kind": "已儲存規則", "score": 0, "count": 0} for text in saved_patterns - known)
                def render():
                    if generation != load_generation["value"] or not top.winfo_exists():
                        return
                    review_identity.update(
                        raw_fingerprint=reports[0]["raw_fingerprint"],
                        scrape_run_id=task["scrape_run_id"], cover_run_id=task.get("cover_run_id"),
                    )
                    rows.clear(); row_order.clear(); chapter_texts.clear()
                    chapter_texts.update(reports[0].get("chapter_texts") or {})
                    chapter_choice.config(values=sorted(chapter_texts, key=int))
                    for index, item in enumerate(sorted(candidates, key=lambda x: -float(x.get("score") or 0))):
                        iid = str(index); item["approved_remove"] = item["text"] in saved_patterns; rows[iid] = item; row_order.append(iid)
                    refresh_tree()
                    refresh_samples()
                    approve_button.config(state=tk.NORMAL)
                    retry_button.config(state=tk.NORMAL)
                    status.set(f"已載入 {len(reports)} 份報告，共 {len(candidates)} 項候選。")
                self.root.after(0, render)
            except Exception as error:
                def show_error(detail=str(error)):
                    if generation != load_generation["value"] or not top.winfo_exists():
                        return
                    status.set(f"載入失敗：{detail}")
                    retry_button.config(state=tk.NORMAL)
                    approve_button.config(state=tk.DISABLED)
                self.root.after(0, show_error)

        def start_load():
            load_generation["value"] += 1
            generation = load_generation["value"]
            retry_button.config(state=tk.DISABLED)
            approve_button.config(state=tk.DISABLED)
            status.set("正在下載廣告分析結果…")
            threading.Thread(target=worker, args=(generation,), daemon=True).start()

        retry_button.config(command=start_load)
        sample_refresh["callback"] = self.open_text_sample(parent=sample_page, task=task)
        start_load()

    @staticmethod
    def _ad_review_sort_value(item, column):
        if column == "decision":
            return int(bool(item.get("approved_remove")))
        if column in {"count", "score"}:
            try:
                return float(item.get(column) or 0)
            except (TypeError, ValueError):
                return float("-inf")
        if column == "chapters":
            result = []
            for value in item.get("affected_chapters") or []:
                try:
                    result.append((0, int(value)))
                except (TypeError, ValueError):
                    result.append((1, str(value).casefold()))
            return tuple(result)
        return str(item.get(column) or "").casefold()

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

    def _load_or_generate_cover_information(self, book_title, catalog_url, use_cache=True, progress_callback=None):
        """Load cached cover information or generate it for the batch GUI."""
        project_root = os.path.dirname(os.path.abspath(__file__))
        info_file = cache_path(project_root, book_profile_id(catalog_url)).parent / "cover_information.json"
        if use_cache and info_file.is_file():
            try:
                cached = json.loads(info_file.read_text(encoding="utf-8"))
                if self._valid_cover_information(cached):
                    return cached, True
            except (OSError, ValueError):
                pass
        data = build_cover_information(book_title, catalog_url=catalog_url, progress_callback=progress_callback)
        info_file.parent.mkdir(parents=True, exist_ok=True)
        info_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data, False

    def _upload_manual_cover(self, book_title, catalog_url, source_path):
        """Normalize and sync a manual cover selected from the batch dialog."""
        project_root = os.path.dirname(os.path.abspath(__file__))
        profile_id = book_profile_id(catalog_url)
        local_cover = cache_path(project_root, profile_id)
        details = normalize_manual_cover(source_path, local_cover)
        store, repo, github_token = self._profile_store()
        cloud = upload_github_cover(local_cover, profile_id, repo, github_token)
        record = {"source": "manual_upload", **cloud, **details}
        store.mutate(
            lambda data: update_book_profile(data, catalog_url, book_title, manual_cover=record),
            f"Update manual cover for {book_title}",
        )
        if self.url_entry.get().strip() == catalog_url:
            self.current_book_profile["manual_cover"] = record
        return local_cover, profile_id, repo, details, cloud

    @staticmethod
    def _register_file_drop(widgets, callback):
        """Register TkDND file drops and report whether registration succeeded."""
        try:
            from tkinterdnd2 import DND_FILES
        except ImportError:
            return False
        registered = False
        for widget in widgets:
            if not hasattr(widget, "drop_target_register"):
                continue
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", callback)
                registered = True
            except (AttributeError, tk.TclError):
                continue
        return registered

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
                local_cover = cache_path(os.path.dirname(os.path.abspath(__file__)), book_profile_id(task.get("catalog_url") or ""))
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
                self.btn_cancel.config(state=tk.NORMAL if task.get("workflow_phase") != "preflight" and task.get("status") in {"running", "dispatching", "processing", "waiting_retry"} else tk.DISABLED)
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
                statuses <= {"running", "dispatching", "preparing_assets", "processing", "waiting_retry"}
            )
        )
        self.btn_stop_task.config(
            state=tk.NORMAL if has_cancellable_run else tk.DISABLED,
            text="正在取消…" if statuses == {"canceling"} else "取消本次 Run",
        )
        if hasattr(self, "btn_toggle_completed"):
            if not tasks:
                self.btn_toggle_completed.config(state=tk.DISABLED, text="標記為已完成")
            else:
                is_in_success_tree = hasattr(self, "queue_tree") and self.queue_tree is getattr(self, "success_queue_tree", None)
                all_completed = (statuses == {"completed"}) or is_in_success_tree
                self.btn_toggle_completed.config(
                    state=tk.NORMAL,
                    text="移回未完成" if all_completed else "標記為已完成",
                )
        if hasattr(self, "btn_batch_cover_info"):
            cover_ready = len(tasks) == 1 and bool(tasks[0].get("cover_run_id")) and \
                ((tasks[0].get("stages") or {}).get("cover") or {}).get("status") == "completed"
            self.btn_batch_cover_info.config(state=tk.NORMAL if cover_ready else tk.DISABLED)
        ad_ready = len(tasks) == 1 and bool(tasks[0].get("scrape_run_id")) and \
            ((tasks[0].get("stages") or {}).get("scrape") or {}).get("status") == "completed"
        self.btn_sample_text.config(state=tk.NORMAL if ad_ready else tk.DISABLED)
        if hasattr(self, "btn_start_processing"):
            reviewed = len(tasks) == 1 and (tasks[0].get("ad_review") or {}).get("status") == "approved"
            ready = reviewed and tasks[0].get("workflow_phase") == "preflight" and tasks[0].get("status") == "waiting_review"
            self.btn_start_processing.config(state=tk.NORMAL if ready else tk.DISABLED)

    def start_selected_processing(self):
        task = self._selected_task()
        if not task:
            return
        self.btn_start_processing.config(state=tk.DISABLED)
        def worker():
            try:
                queue_store, _, _ = self._queue_store()
                queue = queue_store.mutate(
                    lambda data: start_reviewed_processing(data, task["task_id"]),
                    f"Queue reviewed processing for {task['task_id']}",
                )
                self.cloud_queue = queue
                self._dispatch_queue_workflow()
                self.root.after(0, lambda: (
                    self._render_queue(queue),
                    self.log(f"🚀 《{task.get('book_title')}》已排入第二階段後製。"),
                ))
            except Exception as error:
                self.root.after(0, lambda detail=str(error): (
                    messagebox.showerror("無法開始後製", detail, parent=self.root),
                    self._update_queue_control_states(self._selected_tasks()),
                ))
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _text_sample_chapters(task, catalog):
        """Return first/lower-middle/last chapters after applying task filters."""
        total = int(catalog.get("total_chapters") or len(catalog.get("chapters") or []))
        start = max(1, int(task.get("start_chapter") or 1))
        end = min(total, int(task.get("end_chapter") or total))
        excluded = {int(value) for value in task.get("excluded_chapters") or []}
        requested = [int(value) for value in task.get("chapter_order") or []]
        ordered = []
        seen = set()
        for value in requested + list(range(start, end + 1)):
            if start <= value <= end and value not in seen:
                ordered.append(value)
                seen.add(value)
        source_indices = [value for value in ordered if value not in excluded]
        if not source_indices:
            raise ValueError("這項任務的章節範圍已全部排除，沒有可抽查的章節。")
        positions = [0, (len(source_indices) - 1) // 2, len(source_indices) - 1]
        labels = ["第一章", "中間章", "最後一章"]
        samples = []
        for label, position in zip(labels, positions):
            source_index = source_indices[position]
            output_index = position + 1 if task.get("renumber_selected") or requested else source_index
            samples.append({
                "label": label,
                "source_index": source_index,
                "output_index": output_index,
                "url": urljoin(catalog["base_url"], catalog["chapters"][source_index - 1]),
                "catalog_title": catalog["chapter_titles"][source_index - 1],
            })
        return samples

    @staticmethod
    def _build_text_sample(raw_title, raw_body, book_title, remove_patterns=None):
        raw_text = raw_title + "\n\n" + raw_body
        cleaned = clean_text_content(raw_body, raw_title, book_title, remove_patterns=remove_patterns)
        return raw_text, chunk_text(cleaned)

    def _open_cleaner_patterns_dialog(self, task, parent, on_applied):
        if task.get("scrape_run_id"):
            self.open_ad_analysis_results()
            return
        dialog = tk.Toplevel(parent)
        dialog.title(f"《{task.get('book_title') or '待解析'}》刪除關鍵字設定")
        dialog.geometry("900x520")
        dialog.minsize(720, 440)
        dialog.transient(parent)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        status_var = tk.StringVar(
            value="貼入要刪除的完整原文（特殊符號視為普通文字）；每條最多 10,000 字。"
        )
        ttk.Label(body, textvariable=status_var).pack(anchor=tk.W, pady=(0, 8))
        add_row = ttk.Frame(body)
        add_row.pack(fill=tk.X)
        add_row.columnconfigure(0, weight=1)
        entry = scrolledtext.ScrolledText(
            add_row, height=5, wrap=tk.WORD, font=("Microsoft JhengHei", 10),
        )
        entry.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        listbox = tk.Listbox(body, selectmode=tk.EXTENDED, font=("Microsoft JhengHei", 10))
        listbox.pack(fill=tk.BOTH, expand=True, pady=8)
        patterns = list(self.cleaner_remove_patterns)

        def render():
            listbox.delete(0, tk.END)
            for pattern in patterns:
                summary = pattern.replace("\n", " ↵ ")
                if len(summary) > 120:
                    summary = summary[:117] + "…"
                listbox.insert(tk.END, f"{len(pattern):,} 字｜{summary}")

        def add_pattern():
            value = entry.get("1.0", "end-1c")
            if not value.strip():
                return
            try:
                validated = validate_remove_patterns(patterns + [value])
            except ValueError as error:
                messagebox.showwarning("刪除關鍵字", str(error), parent=dialog)
                return
            patterns[:] = validated
            entry.delete("1.0", tk.END)
            render()

        def delete_selected():
            selected = set(listbox.curselection())
            patterns[:] = [value for index, value in enumerate(patterns) if index not in selected]
            render()

        ttk.Button(add_row, text="＋新增", command=add_pattern).grid(
            row=0, column=1, sticky="ns"
        )
        entry.bind("<Control-Return>", lambda _event: (add_pattern(), "break")[1])
        actions = ttk.Frame(body)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="刪除選取項目", command=delete_selected).pack(side=tk.LEFT)
        finish_button = ttk.Button(actions, text="套用", style="Accent.TButton")
        finish_button.pack(side=tk.RIGHT)
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))

        def finish_editing():
            try:
                validated = validate_remove_patterns(patterns)
            except ValueError as error:
                messagebox.showwarning("刪除關鍵字", str(error), parent=dialog)
                return
            self.cleaner_remove_patterns = list(validated)
            on_applied(self.cleaner_remove_patterns)
            dialog.destroy()

        finish_button.config(command=finish_editing)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        render()
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2)
        y = max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        entry.focus_set()

    def open_text_sample(self, parent=None, task=None):
        task = task or self._selected_task()
        if not task:
            messagebox.showinfo("抽查文字", "請先單選一本小說。")
            return

        if parent is None:
            top = tk.Toplevel(self.root)
            top.title(f"文字清理／關鍵字審核｜{task.get('book_title') or '待解析'}")
            top.geometry("1120x760")
            top.minsize(820, 520)
            parent = top
        else:
            top = parent.winfo_toplevel()
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        status_var = tk.StringVar(value="正在解析目錄並取得三個取樣章節…")
        ttk.Label(frame, textvariable=status_var, style="Header.TLabel").pack(anchor=tk.W, pady=(0, 8))
        notebook = ttk.Notebook(frame)
        notebook.pack(fill=tk.BOTH, expand=True)
        views = {}
        preview_generation = {"value": 0}
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

        embedded_review = parent is not top and isinstance(parent.master, ttk.Notebook)
        manage_button = ttk.Button(
            frame, text="管理排除關鍵字",
            command=(lambda: parent.master.select(0)) if embedded_review else
                    (lambda: self._open_cleaner_patterns_dialog(task, top, refresh_preview)),
        )
        manage_button.place(relx=1.0, y=34, anchor=tk.NE)

        def show_error(detail):
            status_var.set("抽查失敗")
            messagebox.showerror("抽查文字失敗", detail, parent=top)

        def render(results, generation):
            if generation != preview_generation["value"]:
                return
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
                    # ScrolledText ignores delete/insert while disabled. Re-enable it
                    # before every preview refresh, then return it to read-only mode.
                    box.config(state=tk.NORMAL)
                    box.delete("1.0", tk.END)
                    box.insert("1.0", content)
                    box.config(state=tk.DISABLED)
            status_var.set(f"抽查完成：3 個位置，{warnings} 個需要留意。左右內容可直接捲動比對。")

        def worker(remove_patterns, generation):
            try:
                catalog = parse_catalog(task.get("catalog_url") or "")
                profiles, _ = self._profile_store()[0].load()
                _, profile = get_book_profile(profiles, task.get("catalog_url") or "", task.get("book_title") or "")
                apply_chapter_title_overrides(
                    catalog, profile.get("chapter_title_overrides") or task.get("chapter_title_overrides") or {},
                    profile.get("chapter_normalized_number_overrides") or
                    task.get("chapter_normalized_number_overrides") or {},
                )
                samples = self._text_sample_chapters(task, catalog)
                results = []
                for sample in samples:
                    title, body = fetch_chapter_text(sample["url"])
                    raw_text, clean_text = self._build_text_sample(
                        title, body, task.get("book_title") or catalog.get("book_title") or "",
                        remove_patterns=remove_patterns,
                    )
                    results.append((sample, raw_text, clean_text))
                self.root.after(
                    0, lambda: render(results, generation) if top.winfo_exists() else None,
                )
            except Exception as error:
                self.root.after(
                    0,
                    lambda detail=str(error): show_error(detail)
                    if top.winfo_exists() and generation == preview_generation["value"] else None,
                )
        def refresh_preview(patterns):
            preview_generation["value"] += 1
            generation = preview_generation["value"]
            status_var.set("關鍵字草稿已套用；正在重新取得三個取樣章節…（按下更新章節設定後才會儲存）")
            threading.Thread(target=worker, args=(list(patterns), generation), daemon=True).start()

        refresh_preview(self.cleaner_remove_patterns)
        return refresh_preview

