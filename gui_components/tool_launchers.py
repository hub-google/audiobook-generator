"""Embedded merge/upload and YouTube supplement panels for the main Tk app.

Panels are constructed lazily, only when selected by the operator. There are
no separate Tk roots, subprocesses or duplicated login/session startup paths
in the main control center.
"""
import importlib

from .runtime import ttk, tk, messagebox

TOOLS = {
    "merge": ("合併／上傳", "合併上傳.gui", "MergeUploadGUI"),
    "youtube": ("YouTube 補件", "tools.youtube_backfill_gui", "App"),
}


class ToolLauncherMixin:
    def _build_tools_tab_ui(self, parent):
        self.tool_notebook = ttk.Notebook(parent)
        self.tool_notebook.pack(fill=tk.BOTH, expand=True)
        self._tool_panels = {}
        self._tool_instances = {}
        for tool_id, (title, _, _) in TOOLS.items():
            panel = ttk.Frame(self.tool_notebook, padding=6)
            self._tool_panels[tool_id] = panel
            self.tool_notebook.add(panel, text=title)
            prompt = ttk.Frame(panel)
            prompt.pack(fill=tk.BOTH, expand=True)
            ttk.Label(
                prompt,
                text=f"{title}：使用 AUDIO 共用視窗，不另外開啟程式。",
            ).pack(pady=(30, 12))
            ttk.Button(
                prompt, text=f"載入{title}",
                command=lambda tool=tool_id: self._launch_auxiliary_tool(tool),
            ).pack()
        self.tool_notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _event: self._launch_auxiliary_tool(
                list(TOOLS)[self.tool_notebook.index(self.tool_notebook.select())]
            ),
        )

    def _launch_auxiliary_tool(self, tool_id):
        if tool_id not in TOOLS:
            raise ValueError("Unknown AUDIO auxiliary tool")
        if tool_id in self._tool_instances:
            self.tool_notebook.select(self._tool_panels[tool_id])
            return True
        title, module_name, class_name = TOOLS[tool_id]
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
            panel = self._tool_panels[tool_id]
            widget = cls(panel)
            for child in list(panel.winfo_children()):
                if child is not widget:
                    child.destroy()
            widget.pack(fill=tk.BOTH, expand=True)
            self._tool_instances[tool_id] = widget
            self.tool_notebook.select(panel)
        except Exception as error:
            messagebox.showerror("無法載入工具", f"{title} 載入失敗：{error}")
            return False
        self.log(f"已載入 {title} 至 AUDIO 主控制台")
        return True
