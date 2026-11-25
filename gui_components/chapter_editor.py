"""Catalog parsing and chapter selection editor."""
from .runtime import *  # noqa: F401,F403
from .chapter_filter_dialog import ChapterFilterDialogMixin

class ChapterEditorMixin(ChapterFilterDialogMixin):
    # ── 解析目錄 ──
    def start_parse_catalog(self):
        url = self.url_entry.get().strip()
        try:
            source, canonical = normalize_source_url(url)
        except (ValueError, SourceParseError) as exc:
            messagebox.showwarning("不支援的來源網址", str(exc))
            return
        if self.editing_task_id:
            task = self._selected_task()
            if task and normalize_source_url(task.get("catalog_url") or "")[1] != canonical:
                messagebox.showwarning("更新章節", "編輯既有任務時不能換書，請先改為「新增小說」。")
                return
        self.catalog_load_token += 1
        token = self.catalog_load_token
        self.btn_parse.config(state=tk.DISABLED)
        self.lbl_status.config(text="解析目錄中...", foreground="#e1b12c")
        self.log(f"正在解析 {source_label(source.source_id)}：{canonical} ...")

        def _worker():
            try:
                res = parse_catalog(canonical)
                canonical_url = res.get("catalog_url", canonical)
                profile = {}
                try:
                    profiles, _ = self._profile_store()[0].load()
                    _, profile = get_book_profile(
                        profiles, canonical_url, res.get("book_title") or "",
                    )
                except Exception as profile_error:
                    # Catalog parsing and the first-chapter preview should
                    # work even when GitHub book settings are temporarily
                    # unreachable. Queue writes still need GitHub credentials.
                    detail = str(profile_error)
                    self.root.after(
                        0,
                        lambda message=detail: self.log(
                            f"⚠ 目錄已取得，但無法讀取雲端書籍設定：{message}；"
                            "可以先查看章節或試抓首章，加入佇列時仍需 GitHub 連線。"
                        ),
                    )
                apply_chapter_title_overrides(
                    res, profile.get("chapter_title_overrides") or {},
                    profile.get("chapter_normalized_number_overrides") or {},
                )
                self.root.after(
                    0,
                    lambda: self._on_parse_success(res, profile)
                    if token == self.catalog_load_token and self.url_entry.get().strip() == url
                    else self.btn_parse.config(state=tk.NORMAL),
                )
            except Exception as e:
                error_message = str(e)
                self.root.after(
                    0,
                    lambda message=error_message: self._on_parse_failed(message)
                    if token == self.catalog_load_token and self.url_entry.get().strip() == url
                    else self.btn_parse.config(state=tk.NORMAL),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def _on_parse_success(self, res, profile=None):
        self.btn_parse.config(state=tk.NORMAL)
        if res and res.get("success"):
            if res.get("catalog_url") and self.url_entry.get().strip() != res["catalog_url"]:
                self.url_entry.delete(0, tk.END)
                self.url_entry.insert(0, res["catalog_url"])
            self.catalog_data = res
            if hasattr(self, "source_choice_var"):
                self.source_choice_var.set(source_label(res.get("source_id")))
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
                text=f"來源: {source_label(res.get('source_id'))} | 書名: {book_title} | 總章節: {total} 章 | 重複章節: {duplicate_count} 章",
                foreground="#27ae60",
            )
            self.entry_start.delete(0, tk.END)
            self.entry_start.insert(0, "1")
            self.entry_end.delete(0, tk.END)
            self.entry_end.insert(0, str(total))
            
            self.btn_filter.config(state=tk.NORMAL)
            if hasattr(self, "_on_catalog_url_changed"):
                self._on_catalog_url_changed()
            self.excluded_chapters.clear()
            self.renumber_selected_chapters = True
            self.chapter_order = list(range(1, total + 1))
            self._update_chapter_selection_summary(1, total)

            self.lbl_status.config(text="解析完成", foreground="#27ae60")
            self.log(f"✓ 解析成功！書名:【{book_title}】，共找到 {total} 章節；標記 {duplicate_count} 個重複章節。")
        else:
            self._on_parse_failed("無法找到章節內容")

    def _on_parse_failed(self, error_msg):
        self.catalog_data = None
        self.btn_parse.config(state=tk.NORMAL)
        if hasattr(self, "btn_add_queue"):
            self.btn_add_queue.config(state=tk.DISABLED)
        if hasattr(self, "btn_preview_chapter"):
            self.btn_preview_chapter.config(state=tk.DISABLED)
        if hasattr(self, "btn_update_queue"):
            self.btn_update_queue.config(state=tk.DISABLED)
        self.btn_filter.config(state=tk.DISABLED)
        self.lbl_status.config(text="解析失敗", foreground="#e74c3c")
        self.log(f"✗ 目錄解析失敗: {error_msg}")
        messagebox.showerror("解析失敗", f"無法讀取該目錄網址：\n{error_msg}")

    def preview_first_chapter(self):
        """Verify a real chapter, not just an HTML catalog, before queueing."""
        if not catalog_matches_current(self.url_entry.get(), self.catalog_data):
            messagebox.showwarning("試抓首章", "請先解析目前書籍目錄。")
            return
        chapters = self.catalog_data.get("chapters") or []
        if not chapters:
            messagebox.showwarning("試抓首章", "此書尚未取得可用章節。")
            return
        url = chapters[0]
        source_id = self.catalog_data["source_id"]
        book_title = self.catalog_data["book_title"]
        self.btn_preview_chapter.config(state=tk.DISABLED)
        self.log(f"試抓【{source_label(source_id)}】《{book_title}》第一章：{url}")

        def worker():
            try:
                title, body = fetch_chapter_text(url, source_id=source_id)
                if not body.strip():
                    raise RuntimeError("來源正文為空")
                self.root.after(0, lambda: self._show_first_chapter(title, body, book_title))
            except Exception as error:
                detail = str(error)
                self.root.after(0, lambda: (
                    self.log(f"✗ 試抓首章失敗：{detail}"),
                    messagebox.showerror("試抓首章失敗", detail),
                ))
            finally:
                self.root.after(0, lambda: self.btn_preview_chapter.config(
                    state=tk.NORMAL if catalog_matches_current(
                        self.url_entry.get(), self.catalog_data,
                    ) else tk.DISABLED,
                ))
        threading.Thread(target=worker, daemon=True).start()

    def _show_first_chapter(self, title, body, book_title):
        """Text shown here is ephemeral and is not added to the queue."""
        top = tk.Toplevel(self.root)
        top.title(f"{book_title}｜試抓首章")
        top.geometry("760x580")
        top.minsize(520, 360)
        frame = ttk.Frame(top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        char_count = len(re.findall(r"[\u3400-\u9fff]", body))
        ttk.Label(
            frame, text=f"{title}｜{char_count:,} 個中文字｜僅供檢視，不自動加入佇列",
        ).pack(anchor=tk.W, pady=(0, 8))
        text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Microsoft JhengHei", 11))
        text.pack(fill=tk.BOTH, expand=True)
        text.insert("1.0", body)
        text.config(state=tk.DISABLED)
        ttk.Button(frame, text="關閉", command=top.destroy).pack(anchor=tk.E, pady=(8, 0))
        self.log(f"✓ 試抓首章成功：{title}，{char_count:,} 個中文字。")

