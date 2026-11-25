"""Chapter selection and ordering UI; catalog fetching remains in chapter_editor."""
from src.chapter_selection_state import (
    complete_source_order, invert_checked, move_before, output_numbers,
    set_checked, sort_normalized,
)
from .duplicate_review_dialogs import show_duplicate_details, choose_duplicate_conditions
from .runtime import (
    threading, tk, Decimal, InvalidOperation, ttk, messagebox, webbrowser, analyze_duplicate_chapters, find_direct_duplicate_matches, split_chapter_title, normalize_positive_chapter_number, GitHubBookProfileStore, update_book_profile, move_chapter_order,
)

class ChapterFilterDialogMixin:
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

        chapter_order = complete_source_order(total_chapters, self.chapter_order)

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
            return output_numbers(chapter_order, chapter_state, cur_start, cur_end)

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
            show_duplicate_details(
                top, index, titles, chapter_parts,
                duplicate_use_number, duplicate_use_name,
                duplicate_use_number_and_name,
                self.chapter_normalized_number_overrides,
            )

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
            chapter_order[:] = move_before(chapter_order, moving, int(target_row))
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
            set_checked(chapter_state, visible_indices, True)
            _update_listbox()
                
        def _deselect_all():
            set_checked(chapter_state, visible_indices, False)
            _update_listbox()

        def _invert_select():
            invert_checked(chapter_state, visible_indices)
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
            choose_duplicate_conditions(
                self, top, duplicate_use_number, duplicate_use_name,
                duplicate_use_number_and_name, _refresh_duplicates,
                _update_listbox, info_lbl, lambda: len(duplicate_indices),
            )

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
            chapter_order[:] = sort_normalized(chapter_order, chapter_parts)
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

