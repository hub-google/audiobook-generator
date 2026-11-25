"""Reusable Tk chapter table presentation, distinct from chapter selection state."""
from dataclasses import dataclass

from .runtime import tk, ttk


@dataclass
class ChapterTableWidgets:
    top: tk.Toplevel
    info: ttk.Label
    controls: ttk.Frame
    search: ttk.Entry
    tree: ttk.Treeview
    show_all: tk.BooleanVar
    show_duplicates: tk.BooleanVar


def create_chapter_table(parent, count, first, last):
    top = tk.Toplevel(parent)
    top.title("選擇要轉換的章節")
    top.geometry("1280x680")
    top.minsize(1050, 480)
    top.resizable(True, True)
    top.grab_set()
    top.configure(bg="#f5f6fa")

    header = ttk.Frame(top)
    header.pack(fill=tk.X, padx=15, pady=(15, 5))
    ttk.Label(
        header,
        text="請取消勾選「不想轉換」的章節 (點擊列或按空白鍵切換勾選)",
        font=("Microsoft JhengHei", 10, "bold"),
    ).pack(anchor="w")
    info = ttk.Label(
        header,
        text=f"全書共 {count} 章 | 目前範圍：第 {first} ~ {last} 章",
        font=("Microsoft JhengHei", 9), foreground="#666666",
    )
    info.pack(anchor="w", pady=(2, 5))

    controls = ttk.Frame(top)
    controls.pack(fill=tk.X, padx=15, pady=5)
    show_all = tk.BooleanVar(value=True)
    show_duplicates = tk.BooleanVar(value=False)
    ttk.Label(controls, text="🔍 搜尋:").pack(side=tk.LEFT, padx=(0, 4))
    search = ttk.Entry(controls, width=16)
    search.pack(side=tk.LEFT, padx=(0, 10))

    container = ttk.Frame(top)
    container.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
    columns = (
        "uuid", "output_number", "normalized_number",
        "display_number", "chapter_name", "duplicate",
    )
    tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="extended")
    headings = {
        "uuid": "UUID", "output_number": "編號章節數",
        "normalized_number": "網站章節數正規化",
        "display_number": "網站顯示章節數",
        "chapter_name": "章節名稱", "duplicate": "重複標記",
    }
    widths = {
        "uuid": 65, "output_number": 105, "normalized_number": 155,
        "display_number": 145, "chapter_name": 280, "duplicate": 85,
    }
    for column in columns:
        tree.heading(column, text=headings[column])
        tree.column(
            column, width=widths[column], minwidth=70,
            anchor=tk.W if column == "chapter_name" else tk.CENTER,
            stretch=column == "chapter_name",
        )
    yscroll = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
    xscroll = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    yscroll.grid(row=0, column=1, sticky="ns")
    xscroll.grid(row=1, column=0, sticky="ew")
    container.rowconfigure(0, weight=1)
    container.columnconfigure(0, weight=1)
    return ChapterTableWidgets(top, info, controls, search, tree, show_all, show_duplicates)
