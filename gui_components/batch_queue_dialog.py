"""Mixed-site bulk intake UI; task mutation remains in queue_operations."""
from .runtime import (
    threading, tk, ttk, messagebox, scrolledtext, webbrowser, normalize_batch_urls, SourceParseError, fetch_69shuba_full_novels, parse_catalog, add_tasks, new_task,
)

class BatchQueueDialogMixin:
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
            text="支援 9 個小說網站，可混合貼入書籍、目錄或章節網址，每行一筆。\n"
                 "自動辨識網站、轉為正確目錄並排除同一本書重複網址；全部預設製作全書。"
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

        def submit():
            urls = []
            for n in novels_data:
                if n["checked"]:
                    urls.append(n["catalog_url"])
            manual_lines = [line.strip() for line in text_box.get("1.0", tk.END).splitlines() if line.strip()]
            urls.extend(manual_lines)
            try:
                final_urls = normalize_batch_urls(urls)
            except (ValueError, SourceParseError) as error:
                messagebox.showerror("網址錯誤", str(error), parent=top)
                return

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

