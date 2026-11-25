from .runtime import (
    json, requests, threading, tk, ttk, messagebox, scrolledtext, io, zipfile, chunk_text, clean_text_content, book_profile_id, update_book_profile, validate_remove_patterns, start_reviewed_processing, cache_path, normalize_manual_cover, upload_github_cover, build_cover_information, PROJECT_ROOT,
)
from .ad_review_dialog import AdReviewDialogMixin
from .cover_preflight_dialog import CoverPreflightDialogMixin
from .batch_cover_dialog import BatchCoverDialogMixin


class ReviewMixin(AdReviewDialogMixin, CoverPreflightDialogMixin, BatchCoverDialogMixin):
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

    def _load_or_generate_cover_information(self, book_title, catalog_url, use_cache=True, progress_callback=None):
        """Load cached cover information or generate it for the batch GUI."""
        project_root = PROJECT_ROOT
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
        project_root = PROJECT_ROOT
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

    def open_text_sample(self, parent=None, task=None, chapter_texts=None):
        """Build an artifact-only Raw/Clean viewer and return its rule refresh callback."""
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
        chapter_texts = chapter_texts if chapter_texts is not None else {}
        current_patterns = {"value": list(self.cleaner_remove_patterns)}
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        status_var = tk.StringVar(value="正在等待 artifact 章節文字…")
        ttk.Label(frame, textvariable=status_var, style="Header.TLabel").pack(anchor=tk.W, pady=(0, 8))

        controls = ttk.Frame(frame)
        controls.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(controls, text="查看章節：").pack(side=tk.LEFT)
        chapter_choice = ttk.Combobox(controls, state="readonly", width=18)
        chapter_choice.pack(side=tk.LEFT, padx=(0, 8))
        info_var = tk.StringVar(value="等待載入…")
        ttk.Label(frame, textvariable=info_var).pack(anchor=tk.W, pady=(0, 5))

        panes = ttk.Panedwindow(frame, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        raw_frame = ttk.LabelFrame(panes, text="Raw TXT（artifact 爬蟲輸出）")
        clean_frame = ttk.LabelFrame(panes, text="Clean TXT（TTS 實際輸入）")
        raw_box = scrolledtext.ScrolledText(raw_frame, wrap=tk.WORD, font=("Microsoft JhengHei", 11))
        clean_box = scrolledtext.ScrolledText(clean_frame, wrap=tk.WORD, font=("Microsoft JhengHei", 11))
        raw_box.pack(fill=tk.BOTH, expand=True)
        clean_box.pack(fill=tk.BOTH, expand=True)
        panes.add(raw_frame, weight=1)
        panes.add(clean_frame, weight=1)

        def ordered_chapters():
            def sort_key(value):
                try:
                    return (0, int(value))
                except (TypeError, ValueError):
                    return (1, str(value))
            return sorted(chapter_texts, key=sort_key)

        def render_selected(_event=None):
            selected = chapter_choice.get()
            raw_text = chapter_texts.get(selected)
            if raw_text is None:
                raw_text = chapter_texts.get(int(selected)) if selected.isdigit() else None
            if raw_text is None:
                return
            title, separator, body = raw_text.partition("\n")
            if not separator:
                body = ""
            cleaned = clean_text_content(
                body, title, task.get("book_title") or "",
                remove_patterns=current_patterns["value"],
            )
            clean_text = chunk_text(cleaned)
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
            info_var.set(
                f"artifact 第 {selected} 章｜{title}｜Raw {raw_chars:,} 字｜"
                f"Clean {clean_chars:,} 字｜約清除 {ratio:.1f}%{warning}"
            )
            for box, content in ((raw_box, raw_text), (clean_box, clean_text)):
                box.config(state=tk.NORMAL)
                box.delete("1.0", tk.END)
                box.insert("1.0", content)
                box.config(state=tk.DISABLED)
            status_var.set(
                f"已從 artifact 載入 {len(chapter_texts):,} 章；切換章節與重新清理都不會連線小說網站。"
            )

        def choose_position(position):
            chapters = ordered_chapters()
            if not chapters:
                return
            chapter_choice.set(str(chapters[position]))
            render_selected()

        ttk.Button(controls, text="第一章", command=lambda: choose_position(0)).pack(side=tk.LEFT, padx=2)
        ttk.Button(controls, text="中間章", command=lambda: choose_position((len(ordered_chapters()) - 1) // 2)).pack(side=tk.LEFT, padx=2)
        ttk.Button(controls, text="最後一章", command=lambda: choose_position(-1)).pack(side=tk.LEFT, padx=2)
        embedded_review = parent is not top and isinstance(parent.master, ttk.Notebook)
        ttk.Button(
            controls, text="管理排除關鍵字",
            command=(lambda: parent.master.select(0)) if embedded_review else
                    (lambda: self._open_cleaner_patterns_dialog(task, top, refresh_preview)),
        ).pack(side=tk.RIGHT)
        chapter_choice.bind("<<ComboboxSelected>>", render_selected)

        def refresh_preview(patterns):
            current_patterns["value"] = list(patterns)
            chapters = ordered_chapters()
            chapter_choice.config(values=tuple(str(value) for value in chapters))
            if not chapters:
                status_var.set("artifact 尚未載入，或報告缺少 chapter_texts；不會改用網路爬取。")
                return
            if chapter_choice.get() not in {str(value) for value in chapters}:
                chapter_choice.set(str(chapters[0]))
            render_selected()

        refresh_preview(self.cleaner_remove_patterns)
        return refresh_preview

