from .runtime import *  # noqa: F401,F403


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
