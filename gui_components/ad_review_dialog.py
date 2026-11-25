"""Advertisement classification and artifact review controls."""
from .runtime import (
    json, time, threading, tk, ttk, messagebox, scrolledtext, simpledialog, datetime, get_book_profile, update_book_profile, validate_remove_patterns, confirm_preflight_review,
)

class AdReviewDialogMixin:
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
        kind_filter = tk.StringVar(value="全部類型")
        ttk.Label(filter_bar, text="搜尋：").pack(side=tk.LEFT)
        search_entry = ttk.Entry(filter_bar, textvariable=search_text, width=28); search_entry.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(filter_bar, text="處理狀態：").pack(side=tk.LEFT)
        decision_combo = ttk.Combobox(filter_bar, textvariable=decision_filter,
                                      values=("全部狀態", "僅顯示待去除", "僅顯示已保留"),
                                      state="readonly", width=14)
        decision_combo.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(filter_bar, text="類型：").pack(side=tk.LEFT)
        kind_combo = ttk.Combobox(
            filter_bar, textvariable=kind_filter, values=("全部類型",),
            state="readonly", width=16,
        )
        kind_combo.pack(side=tk.LEFT, padx=(0, 10))
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
            selected_kind = kind_filter.get()
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
                if selected_kind != "全部類型" and item.get("kind") != selected_kind: continue
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

        search_text.trace_add("write", refresh_tree)
        decision_combo.bind("<<ComboboxSelected>>", refresh_tree)
        kind_combo.bind("<<ComboboxSelected>>", refresh_tree)
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
        manual_bar = ttk.Frame(analysis_page, padding=8); manual_bar.pack(fill=tk.X)
        manual_text = tk.StringVar()
        ttk.Entry(manual_bar, textvariable=manual_text, width=35).pack(side=tk.LEFT, padx=(0, 8))
        def add_manual():
            patterns = validate_remove_patterns([manual_text.get()])
            if not patterns: return
            iid = f"manual-{time.time_ns()}"
            rows[iid] = {"text": patterns[0], "approved_remove": True, "reason": "人工新增", "samples": []}
            row_order.append(iid); manual_text.set(""); refresh_tree(); tree.selection_set(iid); tree.see(iid); refresh_samples()
        ttk.Button(manual_bar, text="新增刪除文字", command=add_manual).pack(side=tk.LEFT)
        actions = ttk.Frame(analysis_page, padding=(8, 0, 8, 8)); actions.pack(fill=tk.X)
        ttk.Button(actions, text="☑ 設為去除", command=lambda: set_selected(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="☐ 設為保留", command=lambda: set_selected(False)).pack(side=tk.LEFT, padx=2)
        ttk.Button(actions, text="反選", command=set_selected).pack(side=tk.LEFT, padx=2)
        def set_visible(value):
            for iid in tree.get_children():
                rows[iid]["approved_remove"] = value
            refresh_tree(); refresh_samples()
        ttk.Button(actions, text="全選目前篩選", command=lambda: set_visible(True)).pack(side=tk.LEFT, padx=(12, 2))
        ttk.Button(actions, text="全不選目前篩選", command=lambda: set_visible(False)).pack(side=tk.LEFT, padx=2)
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
                    kinds = sorted({str(item.get("kind") or "") for item in candidates if item.get("kind")})
                    kind_combo.config(values=("全部類型", *kinds))
                    if kind_filter.get() not in kind_combo.cget("values"):
                        kind_filter.set("全部類型")
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
        sample_refresh["callback"] = self.open_text_sample(
            parent=sample_page, task=task, chapter_texts=chapter_texts,
        )
        start_load()

