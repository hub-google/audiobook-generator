"""Launch auxiliary user interfaces from the single AUDIO control center.

The child processes retain their own state until the common GUI/API services
can absorb their workflows. No shell, credentials, or mode-specific state is
duplicated in the launcher.
"""
from pathlib import Path
from .runtime import ttk, tk, messagebox, subprocess, sys, PROJECT_ROOT

TOOLS = {
    "merge": ("合併／上傳", "合併上傳/gui.py"),
    "youtube": ("YouTube 補件", "tools/youtube_backfill_gui.py"),
}


class ToolLauncherMixin:
    def _build_tools_tab_ui(self, parent):
        rows = (
            ("merge", "檢查已完成檔案，合併並送出上傳"),
            ("youtube", "補傳 YouTube 影片或修復發布狀態"),
        )
        for tool_id, detail in rows:
            line = ttk.Frame(parent)
            line.pack(fill=tk.X, pady=(4, 8))
            title, _ = TOOLS[tool_id]
            ttk.Button(line, text=title, width=19,
                       command=lambda tool=tool_id: self._launch_auxiliary_tool(tool)).pack(side=tk.LEFT, padx=(0, 12))
            ttk.Label(line, text=detail).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _launch_auxiliary_tool(self, tool_id):
        if tool_id not in TOOLS:
            raise ValueError("Unknown AUDIO auxiliary tool")
        title, relative = TOOLS[tool_id]
        root = Path(PROJECT_ROOT).resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            messagebox.showerror("無法開啟", f"找不到 {title} 的程式")
            return False
        try:
            subprocess.Popen([sys.executable, str(target)], cwd=str(root))
        except OSError as error:
            messagebox.showerror("無法開啟", f"{title} 啟動失敗：{error}")
            return False
        self.log(f"已開啟 {title} 工具")
        return True
