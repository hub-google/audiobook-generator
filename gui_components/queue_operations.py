from .runtime import *  # noqa: F401,F403


class QueueOperationsMixin:
    def open_selected_task_progress(self, _event=None):
        task = self._selected_task()
        if not task:
            messagebox.showinfo("查看進度", "請先選取一個小說任務。")
            return
        self._open_task_progress(task)

    def _open_task_progress(self, task):
        task_id = task["task_id"]
        existing = self.task_progress_windows.get(task_id)
        if existing and existing["top"].winfo_exists():
            existing["top"].deiconify()
            existing["top"].lift()
            existing["top"].focus_force()
            return

        top = tk.Toplevel(self.root)
        top.title(f"{task.get('book_title') or '小說任務'}｜Run {task.get('run_id') or '等待建立'}")
        top.geometry("900x680")
        top.minsize(720, 480)

        frame = ttk.Frame(top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        title_var = tk.StringVar(value=f"📖 {task.get('book_title') or '待解析'}")
        ttk.Label(frame, textvariable=title_var, style="Title.TLabel").pack(anchor=tk.W)
        summary_var = tk.StringVar(value="正在取得雲端狀態…")
        ttk.Label(frame, textvariable=summary_var, style="Header.TLabel").pack(anchor=tk.W, pady=(5, 8))

        log_box = scrolledtext.ScrolledText(
            frame, background="#1e1e1e", foreground="#dcdcdc", font=("Consolas", 10), wrap=tk.WORD
        )
        log_box.pack(fill=tk.BOTH, expand=True)
        log_box.tag_config("hyperlink", foreground="#4da6ff", underline=1)
        log_box.tag_config("success", foreground="#44bd32")
        log_box.tag_config("warning", foreground="#e1b12c")
        log_box.tag_config("error", foreground="#e84118")
        log_box.tag_bind("hyperlink", "<Enter>", lambda _event: log_box.config(cursor="hand2"))
        log_box.tag_bind("hyperlink", "<Leave>", lambda _event: log_box.config(cursor=""))
        close_event = threading.Event()

        state = {
            "top": top, "task": dict(task), "title_var": title_var, "summary_var": summary_var,
            "log": log_box, "close_event": close_event, "seen": set(), "job_states": {},
            "completed_logs": set(), "last_log_check": {}, "run_announced": None, "link_counter": 0,
        }
        self.task_progress_windows[task_id] = state

        def close_window():
            close_event.set()
            self.task_progress_windows.pop(task_id, None)
            if top.winfo_exists():
                top.destroy()

        top.protocol("WM_DELETE_WINDOW", close_window)
        ttk.Button(frame, text="關閉視窗（不停止雲端任務）", command=close_window).pack(anchor=tk.E, pady=(8, 0))

        self._append_task_log(task_id, f"🚀 任務：{task.get('book_title') or '待解析'}")
        self._append_task_log(
            task_id,
            f"📚 製作範圍：第 {task.get('start_chapter') or 1}～{task.get('end_chapter') or '全部'} 章",
        )
        if task.get("excluded_chapters"):
            self._append_task_log(task_id, f"🚫 排除章節：{', '.join(map(str, task['excluded_chapters']))}")
        if not task.get("run_id"):
            self._append_task_log(task_id, "⏳ 已加入雲端佇列，正在等待前一個任務完成…", "warning")
        threading.Thread(target=self._poll_task_progress, args=(task_id,), daemon=True).start()

    def _append_task_log(self, task_id, message, style=None, url=None):
        state = self.task_progress_windows.get(task_id)
        if not state or state["close_event"].is_set() or not state["top"].winfo_exists():
            return
        box = state["log"]
        timestamp = time.strftime("%H:%M:%S")
        box.insert(tk.END, f"[{timestamp}] ")
        if url:
            box.insert(tk.END, message + "\n")
            state["link_counter"] += 1
            tag = f"url_{state['link_counter']}"
            box.insert(tk.END, url, ("hyperlink", tag))
            box.tag_bind(tag, "<Button-1>", lambda _event, target=url: webbrowser.open(target))
            box.insert(tk.END, "\n")
        else:
            box.insert(tk.END, message + "\n", (style,) if style else ())
        box.see(tk.END)

    def _task_from_current_queue(self, task_id, fallback):
        if self.cloud_queue:
            current = next((item for item in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", []) if item.get("task_id") == task_id), None)
            if current:
                return dict(current)
        return dict(fallback)

    @staticmethod
    def _job_display_status(job):
        status = job.get("status")
        conclusion = job.get("conclusion")
        if status == "completed":
            return {
                "success": "✅ 完成", "failure": "❌ 失敗", "cancelled": "🛑 已取消",
                "skipped": "⏭️ 已略過",
            }.get(conclusion, f"完成（{conclusion or '未知'}）")
        return {"queued": "⏳ 等待中", "in_progress": "⚡ 執行中"}.get(status, status or "未知")

    def _apply_task_snapshot(self, task_id, task, run_data, jobs, marker_events):
        state = self.task_progress_windows.get(task_id)
        if not state or state["close_event"].is_set() or not state["top"].winfo_exists():
            return
        state["task"] = dict(task)
        run_id = run_data.get("id")
        run_url = run_data.get("html_url")
        if state["run_announced"] != run_id:
            state["run_announced"] = run_id
            state["top"].title(f"{task.get('book_title') or '小說任務'}｜Run {run_id}")
            self._append_task_log(task_id, f"🔗 GitHub Run #{run_id}（點擊開啟）", url=run_url)
            self._append_task_log(task_id, f"↻ 目前雲端執行輪次：Run attempt {run_data.get('run_attempt', 1)}")

        setup_jobs = [job for job in jobs if "Parse Catalog" in (job.get("name") or "")]
        worker_jobs = [job for job in jobs if re.search(r"Worker\s+\d+", job.get("name") or "", re.I)]
        completed_workers = sum(
            1 for job in worker_jobs if job.get("status") == "completed" and job.get("conclusion") == "success"
        )
        hf = task.get("hf_progress") or {}
        youtube = task.get("youtube_progress") or {}
        run_status = run_data.get("conclusion") or run_data.get("status") or "未知"
        state["summary_var"].set(
            f"狀態：{run_status}　｜　Workers：{completed_workers}/{len(worker_jobs)}　｜　"
            f"HF：{hf.get('completed', 0)}/{hf.get('total', 0)}　｜　"
            f"YouTube：{youtube.get('completed', 0)}/{youtube.get('total', 0)}"
        )

        ordered_jobs = setup_jobs + worker_jobs + [
            job for job in jobs if job not in setup_jobs and job not in worker_jobs and job.get("conclusion") != "skipped"
        ]
        for job in ordered_jobs:
            name = job.get("name") or "未命名 Job"
            key = (job.get("status"), job.get("conclusion"))
            if state["job_states"].get(name) == key:
                continue
            state["job_states"][name] = key
            display = self._job_display_status(job)
            if "Parse Catalog" in name:
                if job.get("status") == "in_progress":
                    message = "🔍 正在解析小說目錄並建立 Worker 分工…"
                elif job.get("conclusion") == "success":
                    message = f"✅ 目錄解析完成，已建立 {len(worker_jobs)} 個 Worker"
                else:
                    message = f"🔍 目錄解析：{display}"
            else:
                message = f"{name}｜{display}"
            style = "error" if job.get("conclusion") == "failure" else "success" if job.get("conclusion") == "success" else None
            self._append_task_log(task_id, message, style)

            if "Parse Catalog" in name:
                for step in job.get("steps") or []:
                    step_name = step.get("name") or ""
                    if not re.search(r"parse|catalog|matrix|目錄|解析", step_name, re.I):
                        continue
                    step_key = f"setup-step:{step_name}"
                    step_state = (step.get("status"), step.get("conclusion"))
                    if state["job_states"].get(step_key) == step_state:
                        continue
                    state["job_states"][step_key] = step_state
                    self._append_task_log(
                        task_id, f"   └─ {step_name}｜{self._job_display_status(step)}",
                        "error" if step.get("conclusion") == "failure" else
                        "success" if step.get("conclusion") == "success" else None,
                    )

        for marker_key, message, style in marker_events:
            if marker_key not in state["seen"]:
                state["seen"].add(marker_key)
                self._append_task_log(task_id, message, style)

        terminal = run_data.get("status") == "completed"
        terminal_key = ("run_terminal", run_data.get("conclusion"))
        if terminal and terminal_key not in state["seen"]:
            state["seen"].add(terminal_key)
            conclusion = run_data.get("conclusion")
            if conclusion == "success":
                self._append_task_log(task_id, "🎉 雲端任務已全部完成。", "success")
            else:
                self._append_task_log(task_id, f"❌ 雲端任務結束：{conclusion or '未知結果'}", "error")

    @staticmethod
    def _parse_task_log_markers(text, job_id):
        events = []
        for worker, chapters, progress in re.findall(
            r"\[PROGRESS_MARKER\] Worker-(\d+) \| Ch (\S+) (?:complete|done) \((\d+/\d+)\)", text
        ):
            events.append((f"{job_id}:worker:{worker}:{chapters}:{progress}",
                           f"⚡ Worker {worker}｜第 {chapters} 章｜✅ 合成完成（進度 {progress}）", "success"))
        for action, part, chapters, detail in re.findall(
            r"\[API_UPLOAD_MARKER\] (START|DONE) \| Part (\S+) \| Ch (\S+) \| (.+)", text
        ):
            icon = "▶️" if action == "START" else "✅"
            label = "開始上傳" if action == "START" else "上傳完成並加入播放清單"
            events.append((f"{job_id}:youtube:{action}:{part}:{chapters}",
                           f"📤 YouTube Part {part}｜第 {chapters} 章｜{icon} {label}（{detail.strip()}）",
                           "success" if action == "DONE" else None))
        for part in re.findall(r"\[HF_ARCHIVE_MARKER\] DONE \| Part (\d+)", text):
            events.append((f"{job_id}:hf:{part}", f"📦 Hugging Face Part {part}｜✅ 備份完成", "success"))
        for summary_text in re.findall(r"\[RUN_SUMMARY\] (\{[^\r\n]+\})", text):
            try:
                summary = json.loads(summary_text)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if summary.get("kind") == "worker":
                message = f"📋 Worker {summary.get('worker')} 摘要｜完成 {summary.get('completed')}/{summary.get('total')} 章"
            else:
                message = f"📋 YouTube 摘要｜{summary.get('status')}｜完成 {summary.get('completed')}/{summary.get('total')} Parts"
            events.append((f"{job_id}:summary:{summary_text}", message, None))
        return events

    def _poll_task_progress(self, task_id):
        state = self.task_progress_windows.get(task_id)
        if not state:
            return
        close_event = state["close_event"]
        try:
            repo, token = self._github_settings()
            headers = {
                "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            while not close_event.is_set():
                task = self._task_from_current_queue(task_id, state["task"])
                preflight = task.get("workflow_phase") == "preflight"
                run_ids = ([task.get("scrape_run_id"), task.get("cover_run_id")] if preflight
                            else [task.get("run_id")])
                run_ids = list(dict.fromkeys(int(value) for value in run_ids if value))
                if not run_ids:
                    try:
                        store, _, _ = self._queue_store()
                        queue, _ = store.load()
                        task = next((item for item in queue.get("queue", []) + queue.get("completed", []) if item.get("task_id") == task_id), task)
                    except Exception:
                        pass
                    preflight = task.get("workflow_phase") == "preflight"
                    run_ids = ([task.get("scrape_run_id"), task.get("cover_run_id")] if preflight
                                else [task.get("run_id")])
                    run_ids = list(dict.fromkeys(int(value) for value in run_ids if value))
                    if not run_ids:
                        self.root.after(0, lambda tid=task_id, t=dict(task): self._update_waiting_task_summary(tid, t))
                        close_event.wait(10)
                        continue

                completed_run_count = 0
                for run_id in run_ids:
                    run_response = requests.get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}", headers=headers, timeout=15)
                    run_response.raise_for_status()
                    jobs_response = requests.get(
                        f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", headers=headers, timeout=15
                    )
                    jobs_response.raise_for_status()
                    run_data = run_response.json()
                    jobs = jobs_response.json().get("jobs", [])
                    if run_data.get("status") == "completed":
                        completed_run_count += 1
                    marker_events = []
                    # Paint each stage immediately.  Preflight keeps polling
                    # until both the TXT and cover Runs reach a terminal state.
                    self.root.after(
                        0, lambda tid=task_id, t=dict(task), rd=run_data, js=jobs:
                        self._apply_task_snapshot(tid, t, rd, js, [])
                    )
                    now = time.time()
                    for job in jobs:
                        job_id = job.get("id")
                        job_status = job.get("status")
                        if not job_id or job_status != "in_progress":
                            continue
                        if now - state["last_log_check"].get(job_id, 0) < 30:
                            continue
                        state["last_log_check"][job_id] = now
                        try:
                            log_response = requests.get(
                                f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs",
                                headers=headers, timeout=12, allow_redirects=True,
                            )
                            if log_response.status_code == 200:
                                marker_events.extend(self._parse_task_log_markers(log_response.text, job_id))
                        except requests.RequestException:
                            pass
                    if marker_events:
                        self.root.after(
                            0, lambda tid=task_id, t=dict(task), rd=run_data, js=jobs, me=marker_events:
                            self._apply_task_snapshot(tid, t, rd, js, me)
                        )
                if (not preflight and completed_run_count == len(run_ids)) or (preflight and len(run_ids) >= 2 and completed_run_count == len(run_ids)):
                    break
                close_event.wait(10)
        except Exception as error:
            self.root.after(0, lambda tid=task_id, detail=str(error): self._task_progress_error(tid, detail))
            if not close_event.wait(10):
                threading.Thread(target=self._poll_task_progress, args=(task_id,), daemon=True).start()

    def _update_waiting_task_summary(self, task_id, task):
        state = self.task_progress_windows.get(task_id)
        if state and state["top"].winfo_exists():
            state["task"] = dict(task)
            state["summary_var"].set(f"狀態：{task.get('status') or 'queued'}｜等待建立 GitHub Run")

    def _task_progress_error(self, task_id, detail):
        state = self.task_progress_windows.get(task_id)
        if state and state["top"].winfo_exists():
            state["summary_var"].set("暫時無法取得 GitHub 狀態；雲端任務不受影響")
            self._append_task_log(task_id, f"⚠ GitHub 狀態同步失敗：{detail}", "warning")

    def _mutate_queue_async(self, callback, message, success_message=None):
        def worker():
            try:
                store, _, _ = self._queue_store()
                queue = store.mutate(callback, message)
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                try:
                    self._dispatch_queue_workflow()
                except Exception as dispatch_error:
                    self.root.after(0, lambda e=str(dispatch_error): self.log(
                        f"⚠ 佇列已更新，但調度器暫時無法啟動：{e}"
                    ))
                if success_message:
                    self.root.after(0, lambda m=success_message: self.log(m))
            except Exception as error:
                def show_error(detail=str(error)):
                    self._update_queue_control_states(self._selected_task())
                    messagebox.showerror("雲端佇列", detail)
                self.root.after(0, show_error)
        threading.Thread(target=worker, daemon=True).start()

    def _dispatch_queue_workflow(self):
        _, repo, token = self._queue_store()
        response = requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/queue-dispatcher.yml/dispatches",
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": "master"}, timeout=15,
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(f"無法啟動雲端調度器 ({response.status_code}): {response.text}")

    def trigger_queue_dispatcher(self):
        def worker():
            try:
                self._dispatch_queue_workflow()
                self.root.after(0, lambda: self.log("✓ 已啟動一次雲端調度檢查"))
            except Exception as error:
                self.root.after(0, lambda e=str(error): messagebox.showerror("調度器", e))
        threading.Thread(target=worker, daemon=True).start()

    def enqueue_current_task(self):
        if not self.catalog_data:
            messagebox.showwarning("提示", "請先解析小說目錄")
            return
        try:
            start = int(self.entry_start.get())
            end = int(self.entry_end.get())
        except ValueError:
            messagebox.showwarning("提示", "章節範圍必須是數字")
            return
        task = new_task(
            catalog_url=self.url_entry.get().strip(),
            catalog_identity=self.catalog_data.get("catalog_identity", ""),
            book_title=self.catalog_data.get("book_title", ""),
            start_chapter=start,
            end_chapter=end,
            excluded_chapters=sorted(self.excluded_chapters),
            renumber_selected=self.renumber_selected_chapters,
            duplicate_chapter_count=self.catalog_data.get("duplicate_chapter_count"),
            chapter_title_overrides=self.chapter_title_overrides,
            chapter_order=self.chapter_order,
            chapter_normalized_number_overrides=self.chapter_normalized_number_overrides,
        )
        self._mutate_queue_async(
            lambda queue: add_tasks(queue, [task]),
            f"Add {task['book_title']} to audiobook queue",
            f"✓ 已加入任務：{task['book_title']}（第 {start}～{end} 章）",
        )

    def update_selected_task_chapters(self):
        task = self._selected_task()
        if not task or task.get("task_id") != self.editing_task_id:
            messagebox.showinfo("更新章節", "請先從上方雲端小說佇列選取要編輯的小說。")
            return
        try:
            start, end = int(self.entry_start.get()), int(self.entry_end.get())
            total = int((self.catalog_data or {}).get("total_chapters") or 0)
            if start < 1 or end < start or (total and end > total):
                raise ValueError
        except ValueError:
            messagebox.showwarning("更新章節", "請輸入有效且不超過全書章數的章節範圍。")
            return
        active = task.get("status") in {"running", "dispatching", "canceling"}
        task_id, run_id = task["task_id"], task.get("run_id")
        excluded = sorted(value for value in self.excluded_chapters if start <= value <= end)
        try:
            cleaner_patterns = validate_remove_patterns(self.cleaner_remove_patterns)
        except ValueError as error:
            messagebox.showwarning("更新章節", str(error))
            return
        duplicate_detection = dict(self.duplicate_detection)
        chapter_title_overrides = dict(self.chapter_title_overrides)
        chapter_normalized_number_overrides = dict(self.chapter_normalized_number_overrides)
        renumber_selected = bool(self.renumber_selected_chapters)
        original_button_text = self.btn_update_queue.cget("text")
        self.btn_update_queue.config(state=tk.DISABLED, text="⏳ 正在更新雲端佇列…")
        self.selected_status_var.set(
            f"《{task.get('book_title') or '待解析'}》｜正在儲存第 {start}～{end} 章設定…"
        )

        def finish_update(queue, dispatch_error=None):
            self._render_queue(queue)
            self.btn_update_queue.config(state=tk.NORMAL, text=original_button_text)
            updated = next((item for item in queue.get("queue", []) if item.get("task_id") == task_id), None)
            if updated:
                self._update_selected_task_status(updated)
            result = "已停止舊 Run，確認取消後自動重新排程" if active else "已更新雲端佇列"
            self.log(
                f"✓ {task.get('book_title')}：第 {start}～{end} 章與刪除關鍵字設定；{result}"
            )
            if dispatch_error:
                self.log(f"⚠ 章節設定已儲存，但調度器暫時無法啟動：{dispatch_error}")

        def fail_update(detail):
            self.btn_update_queue.config(state=tk.NORMAL, text=original_button_text)
            self._update_selected_task_status(task)
            messagebox.showerror("更新章節失敗", detail)

        def worker():
            completed_steps = []
            try:
                store, repo, token = self._queue_store()
                profile_store, _, _ = self._profile_store()
                profile_store.mutate(
                    lambda data: update_book_profile(
                        data, task.get("catalog_url") or "", task.get("book_title") or "",
                        cleaner_remove_patterns=cleaner_patterns,
                        duplicate_detection=duplicate_detection,
                        chapter_title_overrides=chapter_title_overrides,
                        chapter_normalized_number_overrides=chapter_normalized_number_overrides,
                    ),
                    f"Update book profile for {task.get('book_title') or task_id}",
                )
                completed_steps.append("書籍清理設定")
                if active and run_id:
                    response = requests.post(
                        f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel",
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15,
                    )
                    if response.status_code not in (200, 202, 409):
                        raise RuntimeError(f"取消舊 Run 失敗 ({response.status_code}): {response.text}")
                queue = store.mutate(
                    lambda value: update_task_chapters(
                        value, task_id, start, end,
                        excluded_chapters=excluded,
                        requeue_after_cancel=active,
                        renumber_selected=renumber_selected,
                        duplicate_chapter_count=(self.catalog_data or {}).get("duplicate_chapter_count"),
                        catalog_identity=(self.catalog_data or {}).get("catalog_identity", ""),
                        chapter_title_overrides=chapter_title_overrides,
                        chapter_order=self.chapter_order,
                        chapter_normalized_number_overrides=chapter_normalized_number_overrides,
                    ),
                    f"Update chapter plan for audiobook task {task_id}",
                )
                completed_steps.append("章節範圍與順序")
                self.cloud_queue = queue
                dispatch_error = None
                try:
                    self._dispatch_queue_workflow()
                except Exception as error:
                    dispatch_error = str(error)
                self.root.after(0, lambda q=queue, e=dispatch_error: finish_update(q, e))
            except Exception as error:
                completed = "、".join(completed_steps)
                detail = str(error)
                if completed:
                    detail = f"{detail}\n\n已成功寫入：{completed}。請重試以完成其餘設定。"
                self.root.after(0, lambda message=detail: fail_update(message))
        threading.Thread(target=worker, daemon=True).start()

    def open_batch_queue_dialog(self):
        top = tk.Toplevel(self.root)
        top.title("批量加入小說網址")
        top.geometry("880x640")
        top.minsize(740, 520)

        notebook = ttk.Notebook(top)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 5))

        tab_ranking = ttk.Frame(notebook, padding=10)
        tab_manual = ttk.Frame(notebook, padding=10)
        notebook.add(tab_ranking, text="🏆 69書吧完本小說排行 (勾選加入)")
        notebook.add(tab_manual, text="📝 手動貼上網址")

        # --- Tab 1: Ranking ---
        novels_data = []
        filter_var = tk.StringVar()
        sort_state = {}

        top_bar = ttk.Frame(tab_ranking)
        top_bar.pack(fill=tk.X, pady=(0, 6))

        lbl_ranking_status = ttk.Label(top_bar, text="正在獲取 69書吧完本小說排行…", foreground="#2980b9")
        lbl_ranking_status.pack(side=tk.LEFT)

        filter_bar = ttk.Frame(tab_ranking)
        filter_bar.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(filter_bar, text="🔍 篩選:").pack(side=tk.LEFT, padx=(0, 4))
        ent_filter = ttk.Entry(filter_bar, textvariable=filter_var, width=18)
        ent_filter.pack(side=tk.LEFT, padx=(0, 10))

        lbl_checked_count = ttk.Label(filter_bar, text="已勾選: 0 / 0 部", font=("Microsoft JhengHei UI", 9, "bold"))
        lbl_checked_count.pack(side=tk.RIGHT, padx=5)

        tree_frame = ttk.Frame(tab_ranking)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("check", "rank", "title", "author", "category", "latest")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("check", text="選取", command=lambda: sort_column("check"))
        tree.heading("rank", text="排名", command=lambda: sort_column("rank"))
        tree.heading("title", text="書名", command=lambda: sort_column("title"))
        tree.heading("author", text="作者", command=lambda: sort_column("author"))
        tree.heading("category", text="分類", command=lambda: sort_column("category"))
        tree.heading("latest", text="最新章節", command=lambda: sort_column("latest"))

        tree.column("check", width=55, minwidth=45, anchor=tk.CENTER)
        tree.column("rank", width=55, minwidth=45, anchor=tk.CENTER)
        tree.column("title", width=200, minwidth=140, anchor=tk.W)
        tree.column("author", width=120, minwidth=90, anchor=tk.W)
        tree.column("category", width=85, minwidth=70, anchor=tk.CENTER)
        tree.column("latest", width=260, minwidth=160, anchor=tk.W)

        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=tree_scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def update_summary():
            checked_ranking = sum(1 for n in novels_data if n["checked"])
            total_ranking = len(novels_data)
            lbl_checked_count.config(text=f"已勾選: {checked_ranking} / {total_ranking} 部")

            manual_text = text_box.get("1.0", tk.END).strip()
            manual_count = len([line for line in manual_text.splitlines() if line.strip()])
            total_to_add = checked_ranking + manual_count
            lbl_summary.config(text=f"待加入總計: {total_to_add} 部小說 (排行勾選 {checked_ranking} 部 + 手動輸入 {manual_count} 條)")

        def render_tree():
            query = filter_var.get().strip().lower()
            tree.delete(*tree.get_children())
            for n in novels_data:
                if query:
                    searchable = f"{n['title']} {n['author']} {n['category']}".lower()
                    if query not in searchable:
                        continue
                check_icon = "☑" if n["checked"] else "☐"
                tree.insert("", tk.END, iid=n["book_id"], values=(
                    check_icon,
                    f"#{n['rank']}",
                    n["title"],
                    n["author"],
                    n["category"],
                    n["latest_chapter"],
                ))
            update_summary()

        def toggle_item(item_id):
            for n in novels_data:
                if n["book_id"] == item_id:
                    n["checked"] = not n["checked"]
                    check_icon = "☑" if n["checked"] else "☐"
                    if tree.exists(item_id):
                        tree.set(item_id, "check", check_icon)
                    break
            update_summary()

        def on_tree_click(event):
            region = tree.identify_region(event.x, event.y)
            if region in ("cell", "tree"):
                item_id = tree.identify_row(event.y)
                if item_id:
                    toggle_item(item_id)

        def on_tree_space(event):
            selected = tree.selection()
            if selected:
                toggle_item(selected[0])
            return "break"

        def on_tree_double_click(event):
            item_id = tree.identify_row(event.y)
            if item_id:
                for n in novels_data:
                    if n["book_id"] == item_id:
                        webbrowser.open(n["catalog_url"])
                        break

        tree.bind("<ButtonRelease-1>", on_tree_click)
        tree.bind("<space>", on_tree_space)
        tree.bind("<Double-Button-1>", on_tree_double_click)
        filter_var.trace_add("write", lambda *args: render_tree())

        def sort_column(col):
            reverse = sort_state.get(col, False)
            if col == "check":
                novels_data.sort(key=lambda x: (not x["checked"], x["rank"]))
            elif col == "rank":
                novels_data.sort(key=lambda x: x["rank"], reverse=reverse)
            elif col in ("title", "author", "category"):
                novels_data.sort(key=lambda x: x.get(col, "") or "", reverse=reverse)
            elif col == "latest":
                novels_data.sort(key=lambda x: x.get("latest_chapter", "") or "", reverse=reverse)
            sort_state[col] = not reverse
            render_tree()

        def select_all():
            query = filter_var.get().strip().lower()
            for n in novels_data:
                if not query or query in f"{n['title']} {n['author']} {n['category']}".lower():
                    n["checked"] = True
                    if tree.exists(n["book_id"]):
                        tree.set(n["book_id"], "check", "☑")
            update_summary()

        def select_none():
            query = filter_var.get().strip().lower()
            for n in novels_data:
                if not query or query in f"{n['title']} {n['author']} {n['category']}".lower():
                    n["checked"] = False
                    if tree.exists(n["book_id"]):
                        tree.set(n["book_id"], "check", "☐")
            update_summary()

        def invert_selection():
            query = filter_var.get().strip().lower()
            for n in novels_data:
                if not query or query in f"{n['title']} {n['author']} {n['category']}".lower():
                    n["checked"] = not n["checked"]
                    if tree.exists(n["book_id"]):
                        tree.set(n["book_id"], "check", "☑" if n["checked"] else "☐")
            update_summary()

        btn_select_all = ttk.Button(filter_bar, text="全選", command=select_all)
        btn_select_all.pack(side=tk.LEFT, padx=(0, 4))
        btn_select_none = ttk.Button(filter_bar, text="取消全選", command=select_none)
        btn_select_none.pack(side=tk.LEFT, padx=(0, 4))
        btn_select_invert = ttk.Button(filter_bar, text="反選", command=invert_selection)
        btn_select_invert.pack(side=tk.LEFT, padx=(0, 4))

        def load_ranking_async():
            lbl_ranking_status.config(text="正在獲取 69書吧完本小說排行…", foreground="#2980b9")
            def worker():
                try:
                    if fetch_69shuba_full_novels is None:
                        raise RuntimeError("未載入 69 書吧排行爬蟲模組")
                    fetched = fetch_69shuba_full_novels()
                    for item in fetched:
                        item["checked"] = False
                    def on_done():
                        nonlocal novels_data
                        novels_data = fetched
                        lbl_ranking_status.config(
                            text=f"✓ 已成功載入 {len(fetched)} 部完本小說排行 (點選可切換勾選，雙擊於瀏覽器開啟)",
                            foreground="#27ae60",
                        )
                        render_tree()
                    top.after(0, on_done)
                except Exception as err:
                    def on_err(msg=str(err)):
                        lbl_ranking_status.config(text=f"✗ 載入失敗: {msg}", foreground="#e74c3c")
                    top.after(0, on_err)
            threading.Thread(target=worker, daemon=True).start()

        btn_reload = ttk.Button(top_bar, text="🔄 重新載入", command=load_ranking_async)
        btn_reload.pack(side=tk.RIGHT)

        load_ranking_async()

        # --- Tab 2: Manual ---
        ttk.Label(
            tab_manual,
            text="每行貼上一個小說目錄網址；系統會依行序解析並加入佇列。全部預設製作第 1 章到全書。\n"
                 "支援 69 書吧目錄或介紹頁網址（如 /book/29590.htm），系統會自動正規化為 /book/29590/。"
        ).pack(anchor=tk.W, pady=(0, 8))
        text_box = scrolledtext.ScrolledText(tab_manual, height=14, wrap=tk.WORD, font=("Consolas", 10))
        text_box.pack(fill=tk.BOTH, expand=True)
        text_box.bind("<KeyRelease>", lambda e: update_summary())

        notebook.bind("<<NotebookTabChanged>>", lambda e: update_summary())

        # --- Bottom Bar ---
        bottom_bar = ttk.Frame(top, padding=10)
        bottom_bar.pack(fill=tk.X)

        lbl_summary = ttk.Label(bottom_bar, text="待加入總計: 0 部小說", font=("Microsoft JhengHei UI", 9))
        lbl_summary.pack(side=tk.LEFT)

        def normalize_url(raw_url):
            raw_url = raw_url.strip()
            if not raw_url:
                return ""
            m = re.search(r'69shuba\.com/(?:book|txt)/(\d+)', raw_url)
            if m:
                return f"https://www.69shuba.com/book/{m.group(1)}/"
            return raw_url

        def submit():
            urls = []
            for n in novels_data:
                if n["checked"]:
                    urls.append(n["catalog_url"])
            manual_lines = [line.strip() for line in text_box.get("1.0", tk.END).splitlines() if line.strip()]
            for line in manual_lines:
                norm = normalize_url(line)
                if norm:
                    urls.append(norm)

            final_urls = []
            seen = set()
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    final_urls.append(u)

            if not final_urls:
                messagebox.showwarning("提示", "尚未選取或輸入任何小說網址！\n請從完本排行中勾選小說，或在手動輸入區填入網址。", parent=top)
                return

            top.destroy()
            self.log(f"正在解析並加入 {len(final_urls)} 部小說…")
            def worker():
                tasks = []
                try:
                    for url in final_urls:
                        try:
                            result = parse_catalog(url)
                        except Exception as parse_err:
                            raise RuntimeError(f"解析失敗（{url}）：{parse_err}") from parse_err
                        if not result.get("success"):
                            raise RuntimeError(f"解析失敗（{url}）：{result.get('error')}")
                        tasks.append(new_task(
                            result.get("catalog_url", url), result["book_title"], 1, result["total_chapters"],
                            renumber_selected=True,
                            duplicate_chapter_count=result.get("duplicate_chapter_count"),
                            catalog_identity=result.get("catalog_identity", ""),
                            chapter_order=list(range(1, result["total_chapters"] + 1)),
                        ))
                    store, _, _ = self._queue_store()
                    queue = store.mutate(lambda value: add_tasks(value, tasks), f"Batch add {len(tasks)} audiobook tasks")
                    self.cloud_queue = queue
                    self.root.after(0, lambda: self._render_queue(queue))
                    self._dispatch_queue_workflow()
                    self.root.after(0, lambda: self.log(f"✓ 已依順序加入 {len(tasks)} 部小說"))
                except Exception as error:
                    self.root.after(0, lambda e=str(error): messagebox.showerror("批量加入失敗", e))
            threading.Thread(target=worker, daemon=True).start()

        btn_submit = ttk.Button(bottom_bar, text="依順序加入佇列", style="Accent.TButton", command=submit)
        btn_submit.pack(side=tk.RIGHT, padx=(6, 0))
        btn_cancel = ttk.Button(bottom_bar, text="取消", command=top.destroy)
        btn_cancel.pack(side=tk.RIGHT)

    def move_selected_task(self, delta):
        tasks = self._selected_tasks()
        if tasks:
            task_ids = [task["task_id"] for task in tasks]
            self._mutate_queue_async(
                lambda queue: move_tasks(queue, task_ids, delta),
                f"Move {len(task_ids)} audiobook task(s)",
            )

    def toggle_selected_task(self):
        tasks = self._selected_tasks()
        if not tasks:
            messagebox.showinfo("暫停／恢復", "請先選取一筆或多筆小說任務。")
            return
        if any(task.get("status") not in {"queued", "paused"} for task in tasks):
            messagebox.showinfo("暫停／恢復", "只有等待中的任務可以暫停；執行中的任務請使用「取消本次 Run」。")
            return
        new_status = "queued" if all(task.get("status") == "paused" for task in tasks) else "paused"
        action = "恢復排程" if new_status == "queued" else "暫停排程"
        self.btn_toggle_task.config(state=tk.DISABLED, text=f"正在{action}…")
        self.selected_status_var.set(f"{len(tasks)} 筆小說任務｜正在{action}…")
        task_ids = [task["task_id"] for task in tasks]

        def mutate(queue):
            for task_id in task_ids:
                queue = update_task(queue, task_id, status=new_status)
            return queue

        self._mutate_queue_async(
            mutate,
            f"Set {len(task_ids)} audiobook task(s) to {new_status}",
            f"✓ 已將 {len(task_ids)} 筆小說任務{action}",
        )

    def toggle_completed_selected_task(self):
        tasks = self._selected_tasks()
        if not tasks:
            messagebox.showinfo("手動調整", "請先選取一筆或多筆小說任務。")
            return
        is_in_success_tree = hasattr(self, "queue_tree") and self.queue_tree is getattr(self, "success_queue_tree", None)
        all_completed = all(task.get("status") == "completed" for task in tasks) or is_in_success_tree
        task_ids = [task["task_id"] for task in tasks]

        if all_completed:
            action = "移回未完成"
            if hasattr(self, "btn_toggle_completed"):
                self.btn_toggle_completed.config(state=tk.DISABLED, text=f"正在{action}…")
            self.selected_status_var.set(f"{len(tasks)} 筆小說任務｜正在{action}…")

            def mutate(queue):
                return move_tasks_to_pending(queue, task_ids, status="paused")

            self._mutate_queue_async(
                mutate,
                f"Move {len(task_ids)} audiobook task(s) back to pending queue",
                f"✓ 已將 {len(task_ids)} 筆小說任務{action}（設為暫停狀態）",
            )
        else:
            running_titles = [
                task.get("book_title") or task.get("task_id")
                for task in tasks
                if task.get("status") in {"running", "dispatching"}
            ]
            if running_titles:
                titles_str = "、".join(running_titles[:3])
                if len(running_titles) > 3:
                    titles_str += f" 等 {len(running_titles)} 部"
                if not messagebox.askyesno(
                    "確認標記為已完成",
                    f"選取的小說中包含正在執行中的任務（{titles_str}）。\n"
                    "確定要手動標記為已完成並移至已完成分類嗎？\n\n"
                    "（此操作將任務歸類至已完成，不會自動取消遠端正在運行的 GitHub Run）",
                ):
                    return

            action = "標記為已完成"
            if hasattr(self, "btn_toggle_completed"):
                self.btn_toggle_completed.config(state=tk.DISABLED, text=f"正在{action}…")
            self.selected_status_var.set(f"{len(tasks)} 筆小說任務｜正在{action}…")

            def mutate(queue):
                return mark_tasks_completed(queue, task_ids, conclusion="manual_success")

            self._mutate_queue_async(
                mutate,
                f"Mark {len(task_ids)} audiobook task(s) as completed",
                f"✓ 已將 {len(task_ids)} 筆小說任務{action}",
            )

    def _find_active_task_id(self, exclude_task_id=None):
        for other in (self.cloud_queue or {}).get("queue", []):
            other_id = other.get("task_id")
            if other_id and other_id != exclude_task_id:
                obs = self.github_observations.get(other_id) or {}
                if (
                    (obs.get("kind") == "ok" and obs.get("raw_status") != "completed") or
                    is_task_active(other)
                ):
                    return other_id
        return None

    def requeue_selected_task(self):
        task = self._selected_task()
        if not task:
            return
        if task.get("workflow_phase") == "preflight":
            if task.get("status") == "waiting_review":
                self.open_ad_analysis_results()
                return
            stages = task.get("stages") or {}
            if any((stages.get(name) or {}).get("status") in {"running", "dispatching"} for name in ("scrape", "cover")):
                messagebox.showinfo("重新排程", "請等待目前 TXT／封面 Run 結束後再重試失敗階段。")
                return
            self._mutate_queue_async(
                lambda value: update_task(value, task["task_id"], status="queued", reason=None),
                f"Retry failed preflight stages for {task['task_id']}",
                "已排程重試未完成階段，已完成的 TXT／封面會保留。",
            )
            return
        title = task.get("book_title") or "小說任務"
        run_id = task.get("run_id")
        active_task_id = self._find_active_task_id(exclude_task_id=task.get("task_id"))
        self.selected_status_var.set(f"《{title}》｜正在重新排程…")

        def worker():
            try:
                # 1. 自動取消遠端舊 Run（若存在）
                if run_id:
                    try:
                        _, repo, token = self._queue_store()
                        response = requests.post(
                            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                            timeout=10,
                        )
                        if response.status_code in (200, 202):
                            self.root.after(0, lambda: self.log(f"🛑 已向 GitHub 發送取消 Run {run_id} 要求"))
                        elif response.status_code in (404, 409):
                            self.root.after(0, lambda: self.log(f"ℹ Run {run_id} 已結束或不存在 ({response.status_code})"))
                        else:
                            self.root.after(0, lambda s=response.status_code: self.log(f"⚠ 取消 Run {run_id} 回應 {s}，繼續重新排程"))
                    except Exception as cancel_err:
                        self.root.after(0, lambda err=str(cancel_err): self.log(f"⚠ 取消 Run 異常 ({err})，繼續重新排程"))

                # 2. 直接更新雲端佇列，無條件重新排程
                store, _, _ = self._queue_store()
                queue = store.mutate(
                    lambda value: requeue_task_after_active(value, task["task_id"], active_id=active_task_id),
                    f"Requeue audiobook task {task['task_id']}",
                )
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                self.root.after(0, lambda: self.log(f"✓ 《{title}》已重新排入雲端佇列"))

                # 3. 立即喚醒雲端調度器
                try:
                    self._dispatch_queue_workflow()
                except Exception as dispatch_error:
                    self.root.after(0, lambda e=str(dispatch_error): self.log(
                        f"⚠ 重新排程已完成，但調度器暫時無法啟動：{e}"
                    ))
            except Exception as error:
                self.root.after(0, lambda e=str(error): messagebox.showerror("重新排程失敗", e))

        threading.Thread(target=worker, daemon=True).start()

    def stop_selected_task(self):
        task = self._selected_task()
        if not task:
            messagebox.showinfo("取消本次 Run", "請先選取一筆小說任務。")
            return
        run_ids = list(dict.fromkeys(int(value) for value in (
            task.get("run_id"), task.get("scrape_run_id"), task.get("cover_run_id"), task.get("processing_run_id")
        ) if value))
        run_id = task.get("run_id")
        if not (run_ids or task.get("status") in {"running", "dispatching", "preparing_assets", "processing", "waiting_retry", "canceling"}):
            messagebox.showinfo("取消本次 Run", "這筆任務目前沒有可取消的 Run。")
            return
        title = task.get("book_title") or "小說任務"
        if not messagebox.askyesno("確認取消", f"要取消「{title}」目前這一次 Run 嗎？\n任務會保留，之後可以重新排程。"):
            return
        self.btn_stop_task.config(state=tk.DISABLED, text="正在送出取消…")
        self.selected_status_var.set(f"《{title}》｜正在向 GitHub 送出取消要求…")
        self.log(f"🛑 正在取消 {title} 的 Run {run_id or '（尚未建立）'}…")

        def fail_stop(detail):
            current = self._selected_task()
            self._update_queue_control_states(current)
            if current:
                self._update_selected_task_status(current)
            messagebox.showerror("取消 Run 失敗", detail)

        def worker():
            try:
                store, repo, token = self._queue_store()
                if run_ids:
                    try:
                        failures = []
                        for current_run_id in run_ids:
                            response = requests.post(
                                f"https://api.github.com/repos/{repo}/actions/runs/{current_run_id}/cancel",
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=15,
                            )
                            if response.status_code not in (200, 202, 409):
                                failures.append(f"Run {current_run_id}: {response.status_code}")
                        if failures:
                            raise RuntimeError("取消 Run 失敗：" + "、".join(failures))
                    except Exception as cancel_error:
                        if isinstance(cancel_error, RuntimeError):
                            raise
                        logging.warning(f"Could not cancel run {run_id}: {cancel_error}")
                target_status = (
                    "stopped" if task.get("status") == "waiting_retry" or
                    (not run_ids and task.get("workflow_phase") != "preflight")
                    else "canceling"
                )
                queue = store.mutate(lambda value: update_task(value, task["task_id"], status=target_status, reason="user_cancelled"), f"Stop audiobook task {task['task_id']}")
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                self.root.after(0, lambda: self.log(
                    f"✓ GitHub 已接受 {title} 的取消要求；正在等待 Run 停止。"
                    if run_ids else f"✓ {title} 已停止，不會建立新的 Run。"
                ))
                try:
                    self._dispatch_queue_workflow()
                except Exception as dispatch_error:
                    self.root.after(0, lambda e=str(dispatch_error): self.log(
                        f"⚠ 取消要求已完成，但調度器暫時無法啟動：{e}"
                    ))
            except Exception as error:
                self.root.after(0, lambda e=str(error): fail_stop(e))
        threading.Thread(target=worker, daemon=True).start()

    def delete_selected_task(self):
        tasks = self._selected_tasks()
        if not tasks or not messagebox.askyesno("確認刪除", f"刪除選取的 {len(tasks)} 筆佇列任務？\n執行中的 Run 會送出取消；不會刪除既有 HF 或 YouTube 成品。"):
            return
        task_ids = [task["task_id"] for task in tasks]
        def worker():
            try:
                store, repo, token = self._queue_store()
                for task in tasks:
                    run_id = task.get("run_id")
                    if run_id and task.get("status") in {"running", "dispatching", "waiting_retry", "canceling"}:
                        requests.post(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/cancel", headers={"Authorization": f"Bearer {token}"}, timeout=15)

                def mutate(value):
                    for task_id in task_ids:
                        value = delete_task(value, task_id)
                    return value

                queue = store.mutate(mutate, f"Delete {len(task_ids)} audiobook task(s)")
                self.cloud_queue = queue
                self.root.after(0, lambda: self._render_queue(queue))
                self._dispatch_queue_workflow()
            except Exception as error:
                self.root.after(0, lambda e=str(error): messagebox.showerror("刪除失敗", e))
        threading.Thread(target=worker, daemon=True).start()

