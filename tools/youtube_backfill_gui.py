"""Local GUI for backfilling YouTube info cards and pinned navigation comments.

This tool is intentionally NOT wired into the production upload pipeline.

Flow:
1. Read owned playlists/videos through the authenticated YouTube Studio session.
2. Let the operator pick one playlist; the first playlist item is treated as Part 1.
3. Fast parallel online status detection accurately probes:
   - 資訊卡: 掛載第一集與播放清單卡片
   - 導流留言: 頻道主發布的【小說導流】留言
   - 置頂留言: 該導流留言已處於置頂狀態
4. Batch operation for missing items:
   - Card 1 (0:03): 導流至第一集 (Part 1)
   - Card 2 (0:13): 導流至完整播放清單 (Playlist)
   - 發表導流留言
   - 置頂導流留言
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import tkinter as tk
from dotenv import load_dotenv
from tkinter import messagebox, ttk

try:
    from tools.chrome_cookie_harvester import (
        extract_youtube_cookies,
        save_cookie_to_env,
        BrowserCardWorker,
    )
except ImportError:
    try:
        from chrome_cookie_harvester import (
            extract_youtube_cookies,
            save_cookie_to_env,
            BrowserCardWorker,
        )
    except ImportError:
        extract_youtube_cookies = None
        save_cookie_to_env = None
        BrowserCardWorker = None

CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCIUtGUZ24fMsfzZtydQTsPg")
STUDIO_ORIGIN = "https://studio.youtube.com"
STUDIO_EDIT_ENDPOINT = f"{STUDIO_ORIGIN}/youtubei/v1/video_editor/edit_video"
STATE_PATH = Path(__file__).with_name("youtube_backfill_state.json")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMENT_MARKER = "【小說導流】"
CARD_1_START_MS = 3000   # 0:03 第一集導流資訊卡
CARD_2_START_MS = 13000  # 0:13 完整播放清單資訊卡
CARD_STATE_VERSION = 3
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


# The UI has no direct Studio HTTP implementation; these symbols remain
# re-exported here to preserve existing callers and tests.
from tools.youtube_backfill_backend import (
    PlaylistRow, VideoRow, StateStore, StudioPrivateClient,
    extract_part_number, build_navigation_comment_text,
)

class App(ttk.Frame):
    """YouTube supplement panel; runs inside the main Tk event loop."""
    def __init__(self, master=None) -> None:
        self._owned_root = tk.Tk() if master is None else None
        parent = master if master is not None else self._owned_root
        super().__init__(parent)
        if self._owned_root is not None:
            self._owned_root.title("YouTube 小說資訊卡／置頂留言補登工具")
            self._owned_root.geometry("1160x820")
            self._owned_root.minsize(960, 680)
            self.pack(fill="both", expand=True)
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        self.raw_cookie: str = os.getenv("YOUTUBE_STUDIO_COOKIES", "").strip()
        self.studio_client: StudioPrivateClient | None = None
        self.state_store = StateStore()
        self.playlists: list[PlaylistRow] = []
        self.videos: list[VideoRow] = []
        self.event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self._build_ui()
        self.after(100, self._poll_events)
        self.after(300, self.auto_start)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text=f"目標頻道：{CHANNEL_ID}", font=("TkDefaultFont", 9, "bold")).pack(side="left")
        self.refresh_btn = ttk.Button(top, text="讀取播放清單", command=self.load_playlists)
        self.refresh_btn.pack(side="left", padx=(12, 4))
        self.relogin_btn = ttk.Button(top, text="瀏覽器自動擷取 Cookie", command=self.fetch_cookie_from_chrome)
        self.relogin_btn.pack(side="left", padx=4)
        self.manual_cookie_btn = ttk.Button(top, text="手動貼上 Cookie", command=self.open_manual_cookie_dialog)
        self.manual_cookie_btn.pack(side="left", padx=4)
        self.status_label = ttk.Label(top, text="", foreground="#555555")
        self.status_label.pack(side="left", padx=8)

        # Main Vertical Split: Upper is Playlists/Videos, Lower is Execution Log
        self.main_paned = ttk.Panedwindow(root, orient="vertical")
        self.main_paned.pack(fill="both", expand=True)

        # Upper Horizontal Split: Left is Playlists, Right is Selection/Actions/Videos
        upper_paned = ttk.Panedwindow(self.main_paned, orient="horizontal")
        self.main_paned.add(upper_paned, weight=3)

        # --- Left Frame: Playlists ---
        left = ttk.Frame(upper_paned, padding=(0, 0, 4, 0))
        upper_paned.add(left, weight=1)

        ttk.Label(left, text="播放清單列表 (點選切換)", font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(0, 4))
        tree_left_frame = ttk.Frame(left)
        tree_left_frame.pack(fill="both", expand=True)
        self.playlist_tree = ttk.Treeview(
            tree_left_frame, columns=("count", "id"), show="tree headings", height=12
        )
        self.playlist_tree.heading("#0", text="名稱")
        self.playlist_tree.heading("count", text="影片")
        self.playlist_tree.heading("id", text="Playlist ID")
        self.playlist_tree.column("#0", width=240)
        self.playlist_tree.column("count", width=55, anchor="center")
        self.playlist_tree.column("id", width=140)

        pl_scroll = ttk.Scrollbar(tree_left_frame, orient="vertical", command=self.playlist_tree.yview)
        self.playlist_tree.configure(yscrollcommand=pl_scroll.set)
        self.playlist_tree.pack(side="left", fill="both", expand=True)
        pl_scroll.pack(side="right", fill="y")
        self.playlist_tree.bind("<<TreeviewSelect>>", self._playlist_selected)

        # --- Right Frame: Selection Info, Options, Actions, Video Tree ---
        right = ttk.Frame(upper_paned, padding=(4, 0, 0, 0))
        upper_paned.add(right, weight=2)

        info = ttk.LabelFrame(right, text="選取播放清單", padding=6)
        info.pack(fill="x")
        self.selection_text = tk.StringVar(value="尚未選擇")
        ttk.Label(info, textvariable=self.selection_text, justify="left").pack(anchor="w")

        options = ttk.Frame(right)
        options.pack(fill="x", pady=6)
        self.do_card = tk.BooleanVar(value=True)
        self.do_comment = tk.BooleanVar(value=True)
        self.do_pin = tk.BooleanVar(value=True)
        self.skip_done = tk.BooleanVar(value=True)
        self.include_first = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="資訊卡 (0:03+0:13)", variable=self.do_card).pack(side="left")
        ttk.Checkbutton(options, text="導流留言", variable=self.do_comment).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="置頂留言", variable=self.do_pin).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="跳過已完成", variable=self.skip_done).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="包含第一集", variable=self.include_first).pack(side="left", padx=8)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(0, 6))
        self.start_button = ttk.Button(actions, text="開始批次補登", command=self.start_batch)
        self.start_button.pack(side="left")
        ttk.Button(actions, text="停止", command=self.stop_batch).pack(side="left", padx=8)

        tree_right_frame = ttk.Frame(right)
        tree_right_frame.pack(fill="both", expand=True)
        self.video_tree = ttk.Treeview(
            tree_right_frame, columns=("pos", "video", "card", "comment", "pin"), show="headings", height=12
        )
        for col, title, width in (
            ("pos", "集", 45),
            ("video", "影片", 340),
            ("card", "資訊卡", 75),
            ("comment", "留言", 75),
            ("pin", "置頂", 75),
        ):
            self.video_tree.heading(col, text=title)
            self.video_tree.column(col, width=width, anchor="w" if col == "video" else "center")
        v_scroll = ttk.Scrollbar(tree_right_frame, orient="vertical", command=self.video_tree.yview)
        self.video_tree.configure(yscrollcommand=v_scroll.set)
        self.video_tree.pack(side="left", fill="both", expand=True)
        v_scroll.pack(side="right", fill="y")

        # --- Lower Frame: Execution Log (Always Visible & Resizable) ---
        log_frame = ttk.LabelFrame(self.main_paned, text="執行紀錄 (可拖曳上下調整高度)", padding=6)
        self.main_paned.add(log_frame, weight=1)

        log_top = ttk.Frame(log_frame)
        log_top.pack(fill="x", pady=(0, 4))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_top, text="自動滾動至最新", variable=self.autoscroll_var).pack(side="left")
        ttk.Button(log_top, text="清空紀錄", command=self.clear_log).pack(side="right", padx=(4, 0))
        ttk.Button(log_top, text="複製紀錄", command=self.copy_log).pack(side="right")

        log_text_frame = ttk.Frame(log_frame)
        log_text_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_text_frame,
            height=7,
            state="disabled",
            wrap="word",
            bg="#1e1e1e",
            fg="#e0e0e0",
            insertbackground="white",
            selectbackground="#264f78",
            font=("Consolas", 9),
        )
        log_scroll = ttk.Scrollbar(log_text_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        if getattr(self, "autoscroll_var", None) and self.autoscroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def copy_log(self) -> None:
        self.clipboard_clear()
        content = self.log_text.get("1.0", "end").strip()
        if content:
            self.clipboard_append(content)
            messagebox.showinfo("提示", "執行紀錄已複製到剪貼簿！", parent=self)

    def _run_thread(self, func: Callable[[], None]) -> None:
        threading.Thread(target=func, daemon=True).start()

    def open_manual_cookie_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("手動貼上 YouTube Studio Cookie")
        dialog.geometry("680x380")
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="請在此貼上從瀏覽器複製的 Cookie（需包含 SAPISID、SID 等憑證）：",
            padding=(12, 12, 12, 6)
        ).pack(anchor="w")

        text_box = tk.Text(dialog, height=10, wrap="word")
        text_box.pack(fill="both", expand=True, padx=12, pady=6)
        if self.raw_cookie:
            text_box.insert("1.0", self.raw_cookie)

        btn_box = ttk.Frame(dialog, padding=12)
        btn_box.pack(fill="x")

        def save_and_close() -> None:
            val = text_box.get("1.0", "end").strip()
            if not val:
                messagebox.showwarning("提示", "輸入內容為空。", parent=dialog)
                return
            if save_cookie_to_env:
                save_cookie_to_env(val, env_path=PROJECT_ROOT / ".env")
            self.raw_cookie = val
            self.log("✅ 已手動更新 Cookie 並儲存至 .env！")
            dialog.destroy()
            self.load_playlists(auto_refetch_on_fail=False)

        ttk.Button(btn_box, text="確定儲存並讀取播放清單", command=save_and_close).pack(side="right", padx=6)
        ttk.Button(btn_box, text="取消", command=dialog.destroy).pack(side="right")

    def auto_start(self) -> None:
        self.log("🚀 程式已啟動，開始檢查 YouTube Studio 登入狀態…")
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        self.raw_cookie = os.getenv("YOUTUBE_STUDIO_COOKIES", "").strip()
        if self.raw_cookie:
            self.log("偵測到本機已存有 Cookie，正在向 YouTube Studio 讀取播放清單…")
            self.load_playlists(auto_refetch_on_fail=True)
        else:
            self.log("未偵測到 Cookie，正在自動啟動 Chrome 擷取 YouTube Studio 登入 Cookie…")
            self.fetch_cookie_from_chrome(and_load_playlists=True)

    def fetch_cookie_from_chrome(self, and_load_playlists: bool = True) -> None:
        if not extract_youtube_cookies:
            messagebox.showerror("錯誤", "找不到 chrome_cookie_harvester 模組。")
            return
        self.relogin_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.status_label.configure(text="正在開啟 Chrome 擷取 Cookie…")

        def work() -> None:
            try:
                self.event_queue.put(("log", "正在喚起 Chrome… 若未登入請在視窗內完成 Google 登入。"))
                cookie_str = extract_youtube_cookies(
                    progress_callback=lambda msg: self.event_queue.put(("log", msg))
                )
                if save_cookie_to_env:
                    save_cookie_to_env(cookie_str, env_path=PROJECT_ROOT / ".env")
                self.raw_cookie = cookie_str
                self.event_queue.put(("cookie_fetched", (cookie_str, and_load_playlists)))
            except Exception as exc:
                self.event_queue.put(("error", f"自動獲取 Cookie 失敗: {exc}"))
            finally:
                self.event_queue.put(("enable_buttons", None))

        self._run_thread(work)

    def load_playlists(self, auto_refetch_on_fail: bool = False) -> None:
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        raw_cookie = (self.raw_cookie or os.getenv("YOUTUBE_STUDIO_COOKIES", "")).strip()
        if not raw_cookie:
            self.log("尚未取得 Cookie，正在自動喚起 Chrome 登入擷取…")
            self.fetch_cookie_from_chrome(and_load_playlists=True)
            return

        self.refresh_btn.configure(state="disabled")
        self.status_label.configure(text="正在讀取播放清單…")

        def work() -> None:
            try:
                self.event_queue.put(("log", "正在透過 YouTube Studio 讀取播放清單…"))
                client = StudioPrivateClient(raw_cookie)
                playlists = client.list_playlists()
                self.raw_cookie = raw_cookie
                self.event_queue.put(("playlists", (client, playlists)))
            except Exception as exc:
                err_msg = str(exc)
                self.event_queue.put(("log", f"讀取播放清單失敗: {err_msg}"))
                if auto_refetch_on_fail:
                    self.event_queue.put(("log", "⚠️ 現有 Cookie 可能已失效，正在自動喚起 Chrome 重新擷取 Cookie…"))
                    self.event_queue.put(("auto_refetch", None))
                else:
                    self.event_queue.put(("error", f"讀取播放清單失敗: {err_msg}"))
            finally:
                self.event_queue.put(("enable_buttons", None))

        self._run_thread(work)

    def _playlist_selected(self, _event=None) -> None:
        selection = self.playlist_tree.selection()
        if not selection or not self.playlists:
            return
        try:
            index = int(selection[0])
            row = self.playlists[index]
        except (ValueError, IndexError):
            return

        raw_cookie = (self.raw_cookie or os.getenv("YOUTUBE_STUDIO_COOKIES", "")).strip()
        if not self.studio_client and raw_cookie:
            try:
                self.studio_client = StudioPrivateClient(raw_cookie)
            except Exception as exc:
                self.log(f"初始化 Studio 客戶端失敗: {exc}")
                return

        if not self.studio_client:
            self.log("尚未初始化 Studio 客戶端，請先讀取播放清單。")
            return

        def work() -> None:
            try:
                self.event_queue.put(("log", f"正在讀取播放清單「{row.title}」的影片列表…"))
                videos = self.studio_client.list_playlist_videos(row.playlist_id)
                self.event_queue.put(("videos", (row, videos)))
                # Launch fast parallel online status probing for all videos in this playlist
                self._run_thread(lambda: self._probe_videos_online_status(row, videos))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
        self._run_thread(work)

    def _probe_videos_online_status(self, playlist: PlaylistRow, videos: list[VideoRow]) -> None:
        if not self.studio_client:
            return
        self.event_queue.put(("log", f"🔍 正在極速平行偵測「{playlist.title}」共 {len(videos)} 支影片的即時狀態…"))

        def probe_one(v: VideoRow) -> dict[str, Any]:
            if self.stop_event.is_set():
                return {}
            try:
                return self.studio_client.check_video_online_status(v.video_id, playlist.playlist_id) | {"video_id": v.video_id}
            except Exception:
                return {"video_id": v.video_id, "has_card": False, "has_comment": False, "has_pin": False, "comment_id": ""}

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(probe_one, videos))

        for online in results:
            vid = online.get("video_id")
            if not vid:
                continue
            if online.get("has_card"):
                self.event_queue.put(("status", (vid, "card", "已完成")))
                self.state_store.mark(vid, "has_card", True)
                self.state_store.mark(vid, "card_playlist_id", playlist.playlist_id)
            else:
                self.event_queue.put(("status", (vid, "card", "")))
                self.state_store.mark(vid, "has_card", False)

            if online.get("has_comment"):
                self.event_queue.put(("status", (vid, "comment", "已完成")))
                self.state_store.mark(vid, "has_comment", True)
                if online.get("comment_id"):
                    self.state_store.mark(vid, "comment_id", online["comment_id"])
            else:
                self.event_queue.put(("status", (vid, "comment", "")))
                self.state_store.mark(vid, "has_comment", False)

            if online.get("has_pin"):
                self.event_queue.put(("status", (vid, "pin", "已完成")))
                self.state_store.mark(vid, "has_pin", True)
                if online.get("comment_id"):
                    self.state_store.mark(vid, "pinned_comment_id", online["comment_id"])
            else:
                self.event_queue.put(("status", (vid, "pin", "")))
                self.state_store.mark(vid, "has_pin", False)

        self.event_queue.put(("log", f"✅「{playlist.title}」全部影片線上即時狀態偵測完成！"))

    def start_batch(self) -> None:
        selection = self.playlist_tree.selection()
        if not selection or not self.videos or not self.playlists:
            messagebox.showwarning("尚未選擇", "請先讀取並選擇播放清單。")
            return
        raw_cookie = (self.raw_cookie or os.getenv("YOUTUBE_STUDIO_COOKIES", "")).strip()
        if not raw_cookie:
            messagebox.showwarning("缺少 Cookie", "所有讀寫動作都需要 YouTube Studio Cookie。請點擊『重新登入擷取 Cookie』。")
            return
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        playlist = self.playlists[int(selection[0])]
        videos = list(self.videos)
        # 建立部數映射表以精確獲取上一部、下一部與第一部
        part_map: dict[int, VideoRow] = {}
        for idx, v in enumerate(videos):
            pnum = extract_part_number(v.title, default=idx + 1)
            if pnum is not None and pnum not in part_map:
                part_map[pnum] = v

        first_video = part_map.get(min(part_map.keys())) if part_map else (videos[0] if videos else None)
        first = first_video or videos[0]
        do_card = self.do_card.get()
        do_comment = self.do_comment.get()
        do_pin = self.do_pin.get()
        skip_done = self.skip_done.get()
        include_first = self.include_first.get()

        def work() -> None:
            browser_worker: BrowserCardWorker | None = None
            try:
                studio = StudioPrivateClient(raw_cookie)
                self.studio_client = studio
                for idx, video in enumerate(videos):
                    if self.stop_event.is_set():
                        break
                    if video.position == 0 and not include_first:
                        continue
                    record = self.state_store.video(video.video_id)
                    self.event_queue.put(("log", f"處理第 {video.position + 1} 集: {video.title}"))

                    # --- Step 1: 資訊卡 (0:03 第一集 + 0:13 完整清單) ---
                    if do_card:
                        if skip_done and (record.get("has_card") or record.get("card_playlist_id") == playlist.playlist_id):
                            self.event_queue.put(("status", (video.video_id, "card", "已完成")))
                            self.event_queue.put(("log", f"⏩ 影片「{video.title}」資訊卡已存在，自動跳過。"))
                        else:
                            try:
                                card_saved = False
                                # 1. 優先嘗試純 HTTP 快速通道
                                try:
                                    studio.set_navigation_card(video.video_id, playlist.playlist_id, first.video_id)
                                    card_saved = True
                                except Exception as card_err:
                                    err_str = str(card_err)
                                    if any(kw in err_str for kw in ("challenge", "額外驗證", "403", "401", "身份驗證", "身分驗證")):
                                        self.event_queue.put(("log", "⚠️ Studio API 要求安全驗證，正在喚起 Chrome 瀏覽器安全通道掛載資訊卡…"))
                                        if not browser_worker and BrowserCardWorker:
                                            browser_worker = BrowserCardWorker()
                                        if browser_worker:
                                            browser_worker.set_card(
                                                video.video_id,
                                                playlist.playlist_id,
                                                first.video_id if video.video_id != first.video_id else "",
                                                card1_ms=CARD_1_START_MS,
                                                card2_ms=CARD_2_START_MS,
                                                progress_callback=lambda msg: self.event_queue.put(("log", msg)),
                                            )
                                            card_saved = True
                                        else:
                                            raise card_err
                                    else:
                                        raise card_err

                                if card_saved:
                                    self.state_store.mark(video.video_id, "card_playlist_id", playlist.playlist_id)
                                    self.state_store.mark(video.video_id, "has_card", True)
                                    self.state_store.mark(video.video_id, "card_state_version", CARD_STATE_VERSION)
                                    self.event_queue.put(("status", (video.video_id, "card", "OK")))
                                    self.event_queue.put(("log", f"✅ 影片「{video.title}」資訊卡已成功掛載！"))
                            except Exception as card_fail:
                                self.event_queue.put(("log", f"❌ 影片「{video.title}」資訊卡掛載失敗: {card_fail}"))

                    comment_id = record.get("comment_id", "")
                    has_comment = record.get("has_comment", False) or bool(comment_id)
                    has_pin = record.get("has_pin", False) or bool(record.get("pinned_comment_id"))

                    # --- Step 2: 導流留言 (獨立偵測與執行) ---
                    if do_comment:
                        if skip_done and has_comment:
                            self.event_queue.put(("status", (video.video_id, "comment", "已完成")))
                            self.event_queue.put(("log", f"⏩ 影片「{video.title}」導流留言已存在，自動跳過。"))
                        else:
                            try:
                                pnum = extract_part_number(video.title, default=idx + 1)
                                prev_video = (
                                    part_map.get(pnum - 1)
                                    if (pnum is not None and (pnum - 1) in part_map)
                                    else (videos[idx - 1] if idx > 0 and not part_map else None)
                                )
                                prev_pnum = (
                                    extract_part_number(prev_video.title, default=pnum - 1 if pnum else idx)
                                    if prev_video
                                    else None
                                )

                                next_video = (
                                    part_map.get(pnum + 1)
                                    if (pnum is not None and (pnum + 1) in part_map)
                                    else (videos[idx + 1] if idx + 1 < len(videos) and not part_map else None)
                                )
                                next_pnum = (
                                    extract_part_number(next_video.title, default=pnum + 1 if pnum else idx + 2)
                                    if next_video
                                    else None
                                )

                                comment_text = build_navigation_comment_text(
                                    current_video_id=video.video_id,
                                    playlist_id=playlist.playlist_id,
                                    first_video=first,
                                    next_video=next_video,
                                    prev_video=prev_video,
                                    next_part_num=next_pnum,
                                    prev_part_num=prev_pnum,
                                )
                                comment_id, _ = studio.post_navigation_comment(
                                    video.video_id,
                                    first.video_id,
                                    playlist.playlist_id,
                                    comment_text=comment_text,
                                )
                                self.state_store.mark(video.video_id, "comment_id", comment_id)
                                self.state_store.mark(video.video_id, "has_comment", True)
                                has_comment = True
                                self.event_queue.put(("status", (video.video_id, "comment", "OK")))
                                self.event_queue.put(("log", f"✅ 影片「{video.title}」導流留言已發布！"))
                            except Exception as comment_fail:
                                self.event_queue.put(("log", f"❌ 影片「{video.title}」發布導流留言失敗: {comment_fail}"))

                    # --- Step 3: 置頂留言 (獨立偵測與執行) ---
                    if do_pin:
                        if skip_done and has_pin:
                            self.event_queue.put(("status", (video.video_id, "pin", "已完成")))
                            self.event_queue.put(("log", f"⏩ 影片「{video.title}」置頂留言已完成，自動跳過。"))
                        else:
                            try:
                                if not comment_id:
                                    online = studio.check_video_online_status(video.video_id, playlist.playlist_id)
                                    comment_id = online.get("comment_id", "")
                                if not comment_id:
                                    raise RuntimeError(
                                        f"尚未偵測到導流留言，無法置頂。請先勾選『導流留言』後再試。"
                                    )
                                studio.pin_comment(video.video_id, comment_id)
                                self.state_store.mark(video.video_id, "pinned_comment_id", comment_id)
                                self.state_store.mark(video.video_id, "has_pin", True)
                                self.event_queue.put(("status", (video.video_id, "pin", "OK")))
                                self.event_queue.put(("log", f"📌 影片「{video.title}」導流留言已成功置頂！"))
                            except Exception as pin_fail:
                                self.event_queue.put(("log", f"❌ 影片「{video.title}」置頂留言失敗: {pin_fail}"))

                self.event_queue.put(("done", "批次處理完成。" if not self.stop_event.is_set() else "已停止。"))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
                self.event_queue.put(("done", "批次處理中止。"))
            finally:
                if browser_worker:
                    try:
                        browser_worker.close()
                    except Exception:
                        pass

        self._run_thread(work)

    def stop_batch(self) -> None:
        self.stop_event.set()
        self.log("收到停止指令，會在目前這支影片完成後停止。")

    def _update_status(self, video_id: str, field: str, value: str) -> None:
        for iid in self.video_tree.get_children():
            values = list(self.video_tree.item(iid, "values"))
            if len(values) < 5:
                continue
            hidden_id = self.video_tree.item(iid, "text")
            if hidden_id != video_id:
                continue
            index = {"card": 2, "comment": 3, "pin": 4}[field]
            values[index] = value
            self.video_tree.item(iid, values=values)
            break

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.event_queue.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "error":
                    self.log(f"❌ 錯誤: {payload}")
                    self.status_label.configure(text=f"錯誤: {str(payload)[:30]}")
                    messagebox.showerror("錯誤", str(payload))
                elif kind == "playlists":
                    self.studio_client, self.playlists = payload
                    self.playlist_tree.delete(*self.playlist_tree.get_children())
                    for idx, row in enumerate(self.playlists):
                        self.playlist_tree.insert(
                            "", "end", iid=str(idx), text=row.title,
                            values=(
                                row.item_count if row.item_count >= 0 else "—",
                                row.playlist_id,
                            )
                        )
                    self.log(f"✅ 已成功載入 {len(self.playlists)} 個播放清單。")
                    self.status_label.configure(text=f"已載入 {len(self.playlists)} 個清單")
                    if self.playlists:
                        self.playlist_tree.selection_set("0")
                        self.playlist_tree.focus("0")
                        self._playlist_selected()
                elif kind == "videos":
                    row, self.videos = payload
                    actual_count = len(self.videos)
                    for playlist_index, playlist_row in enumerate(self.playlists):
                        if playlist_row.playlist_id != row.playlist_id:
                            continue
                        playlist_row.item_count = actual_count
                        item_id = str(playlist_index)
                        if self.playlist_tree.exists(item_id):
                            self.playlist_tree.item(
                                item_id,
                                values=(actual_count, playlist_row.playlist_id),
                            )
                        break
                    self.video_tree.delete(*self.video_tree.get_children())
                    for idx, video in enumerate(self.videos):
                        record = self.state_store.video(video.video_id)
                        self.video_tree.insert(
                            "", "end", iid=str(idx), text=video.video_id,
                            values=(
                                video.position + 1,
                                video.title,
                                "已完成" if record.get("has_card") else "",
                                "已完成" if (record.get("has_comment") or record.get("comment_id")) else "",
                                "已完成" if (record.get("has_pin") or record.get("pinned_comment_id")) else "",
                            ),
                        )
                    if self.videos:
                        first = self.videos[0]
                        self.selection_text.set(
                            f"{row.title}\n"
                            f"Playlist ID: {row.playlist_id}\n"
                            f"第一集：{first.title}\n"
                            f"第一集：https://youtu.be/{first.video_id}\n"
                            f"播放清單：https://www.youtube.com/playlist?list={row.playlist_id}"
                        )
                    self.log(f"已載入「{row.title}」共 {len(self.videos)} 支影片。")
                    self.status_label.configure(text=f"已選取「{row.title}」({len(self.videos)} 支影片)")
                elif kind == "status":
                    video_id, field, value = payload
                    self._update_status(video_id, field, value)
                elif kind == "cookie_fetched":
                    cookie_str, and_load = payload
                    self.raw_cookie = cookie_str
                    self.log("✅ 成功獲取 YouTube Studio Cookie 並已自動儲存至 .env！")
                    self.status_label.configure(text="Cookie 獲取成功")
                    if and_load:
                        self.load_playlists(auto_refetch_on_fail=False)
                elif kind == "auto_refetch":
                    self.fetch_cookie_from_chrome(and_load_playlists=True)
                elif kind == "enable_buttons":
                    self.relogin_btn.configure(state="normal")
                    self.refresh_btn.configure(state="normal")
                elif kind == "done":
                    self.log(str(payload))
                    self.status_label.configure(text=str(payload))
                    self.start_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll_events)


def main() -> None:
    app = App()
    app._owned_root.mainloop()


if __name__ == "__main__":
    main()
