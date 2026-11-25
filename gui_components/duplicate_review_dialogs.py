"""Focused duplicate chapter detail and detection-condition dialogs.

The chapter editor owns its selection state; these windows take only explicit
parameters and callbacks instead of capturing the entire 600-line Tk function.
"""
from .runtime import (
    tk, ttk, messagebox, threading, find_direct_duplicate_matches,
    update_book_profile,
)

def show_duplicate_details(top, index, titles, chapter_parts, use_number, use_name, use_number_and_name, overrides):
    matches = find_direct_duplicate_matches(
        titles, index,
        use_normalized_number=use_number.get(),
        use_chapter_name=use_name.get(),
        normalized_number_overrides=overrides,
        use_number_and_name=use_number_and_name.get(),
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


def choose_duplicate_conditions(app, top, use_number, use_name, use_number_and_name, refresh, render, info_lbl, duplicate_count):
    dialog = tk.Toplevel(top)
    dialog.title("重複判斷條件")
    dialog.resizable(False, False)
    dialog.transient(top)
    dialog.grab_set()
    body = ttk.Frame(dialog, padding=15)
    body.pack(fill=tk.BOTH, expand=True)
    pending_use_number = tk.BooleanVar(value=use_number.get())
    pending_use_name = tk.BooleanVar(value=use_name.get())
    pending_use_number_and_name = tk.BooleanVar(value=use_number_and_name.get())
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
        use_number.set(pending_use_number.get())
        use_name.set(pending_use_name.get())
        use_number_and_name.set(pending_use_number_and_name.get())
        app.duplicate_detection = {
            "use_normalized_number": use_number.get(),
            "use_chapter_name": use_name.get(),
            "use_number_and_name": use_number_and_name.get(),
        }
        refresh()
        render()
        info_lbl.config(text=f"已重新判斷重複章節｜共標記 {duplicate_count()} 個")
        dialog.destroy()

        task = app._selected_task()
        if task:
            def save_conditions():
                try:
                    store, _, _ = app._profile_store()
                    store.mutate(
                        lambda data: update_book_profile(
                            data, task.get("catalog_url") or "", task.get("book_title") or "",
                            cleaner_remove_patterns=app.cleaner_remove_patterns,
                            duplicate_detection=app.duplicate_detection,
                            chapter_title_overrides=app.chapter_title_overrides,
                            chapter_normalized_number_overrides=app.chapter_normalized_number_overrides,
                        ),
                        f"Update duplicate detection for {task.get('book_title') or 'audiobook'}",
                    )
                except Exception as error:
                    app.root.after(0, lambda detail=str(error): messagebox.showerror(
                        "儲存重複判斷條件失敗", detail, parent=top,
                    ))
            threading.Thread(target=save_conditions, daemon=True).start()

    ttk.Button(body, text="套用", command=_apply_conditions).pack(anchor=tk.E, pady=(12, 0))

