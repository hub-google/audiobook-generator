"""Inline normalized chapter number and title editors for Tk chapter table.

These editors operate on explicit callbacks, instead of closing over a giant
window builder with implicit mutable cross-function dependencies.
"""
from .runtime import tk, ttk, messagebox, normalize_positive_chapter_number


def edit_chapter_name(app, top, chapter_tree, event, get_parts, titles, refresh, render):
    if chapter_tree.identify_region(event.x, event.y) != "cell" or chapter_tree.identify_column(event.x) != "#5":
        return
    row = chapter_tree.identify_row(event.y)
    if not row:
        return
    index = int(row)
    x, y, width, height = chapter_tree.bbox(row, "chapter_name")
    editor = ttk.Entry(chapter_tree)
    editor.insert(0, get_parts()[index - 1]["chapter_name"])
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
            parts = get_parts()[index - 1]
            full_title = " ".join(value for value in (parts["display_number"], new_name) if value).strip()
            titles[index - 1] = full_title
            app.catalog_data["chapter_titles"][index - 1] = full_title
            app.chapter_title_overrides[str(index)] = full_title
            refresh()
            render()
        editor.destroy()

    editor.bind("<Return>", lambda _event: finish(True))
    editor.bind("<Escape>", lambda _event: finish(False))
    editor.bind("<FocusOut>", lambda _event: finish(True))


def edit_normalized_number(app, top, chapter_tree, event, get_parts, refresh, render, info_lbl):
    if (chapter_tree.identify_region(event.x, event.y) != "cell" or
            chapter_tree.identify_column(event.x) != "#3"):
        return
    row = chapter_tree.identify_row(event.y)
    if not row:
        return
    index = int(row)
    x, y, width, height = chapter_tree.bbox(row, "normalized_number")
    editor = ttk.Entry(chapter_tree)
    editor.insert(0, get_parts()[index - 1]["normalized_number"])
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
                app.chapter_normalized_number_overrides[str(index)] = normalized_value
            else:
                app.chapter_normalized_number_overrides.pop(str(index), None)
            app.catalog_data["chapter_normalized_number_overrides"] = dict(
                app.chapter_normalized_number_overrides
            )
            refresh()
            render()
            info_lbl.config(text=f"UUID {index} 的網站章節數正規化已更新；按排序按鈕才會移動章節")
        editor.destroy()

    editor.bind("<Return>", lambda _event: finish(True))
    editor.bind("<Escape>", lambda _event: finish(False))
    editor.bind("<FocusOut>", lambda _event: finish(True))

