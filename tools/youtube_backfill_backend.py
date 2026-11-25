"""YouTube Studio networking and state for supplementary audiobook metadata.

This service is intentionally NOT wired into the production upload pipeline.

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


@dataclass
class PlaylistRow:
    playlist_id: str
    title: str
    item_count: int
    published_at: str = ""


@dataclass
class VideoRow:
    video_id: str
    title: str
    position: int


def extract_part_number(title: str, default: int | None = None) -> int | None:
    """從影片標題精準提取部數（數字）。
    
    支援格式範例：
    - [已完結]《修真聊天群》第 1501~1600 章【第 18 部】 -> 18
    - 【第18部】 -> 18
    - 第19部 -> 19
    - Part 2 -> 2
    - 【第 3 集】 -> 3
    """
    if not title:
        return default
    patterns = [
        r"【第\s*(\d+)\s*部】",
        r"第\s*(\d+)\s*部",
        r"\[第\s*(\d+)\s*部\]",
        r"\(第\s*(\d+)\s*部\)",
        r"(?:^|\s)Part[_\s]*(\d+)",
        r"【第\s*(\d+)\s*集】",
        r"第\s*(\d+)\s*集",
    ]
    for pat in patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, TypeError):
                pass
    return default


def build_navigation_comment_text(
    current_video_id: str,
    playlist_id: str,
    first_video: VideoRow | None = None,
    next_video: VideoRow | None = None,
    prev_video: VideoRow | None = None,
    next_part_num: int | None = None,
    prev_part_num: int | None = None,
) -> str:
    """組裝標準化置頂導流留言格式。
    
    格式範例：
    🎧 【從第1部開始聽】：https://www.youtube.com/watch?v=g9Ku5qHqKsk
    ▶️【下一部 第19部】：https://www.youtube.com/watch?v=xIsqFbnTzME
    ⏪【上一部 第17部】：https://www.youtube.com/watch?v=WAN53AmDn84
    📚  完整播放清單：https://www.youtube.com/playlist?list=PLYHOe8Vx5qQI
    """
    lines: list[str] = []

    # 1. 🎧 【從第1部開始聽】 (若有第 1 部且當前影片並非第 1 部)
    if first_video and first_video.video_id and first_video.video_id != current_video_id:
        lines.append(f"🎧 【從第1部開始聽】：https://www.youtube.com/watch?v={first_video.video_id}")

    # 2. ▶️【下一部 第X部】 (若有下一部)
    if next_video and next_video.video_id and next_video.video_id != current_video_id:
        next_label = f"第{next_part_num}部" if next_part_num is not None else "下一部"
        lines.append(f"▶️【下一部 {next_label}】：https://www.youtube.com/watch?v={next_video.video_id}")

    # 3. ⏪【上一部 第Y部】 (若有上一部)
    if prev_video and prev_video.video_id and prev_video.video_id != current_video_id:
        prev_label = f"第{prev_part_num}部" if prev_part_num is not None else "上一部"
        lines.append(f"⏪【上一部 {prev_label}】：https://www.youtube.com/watch?v={prev_video.video_id}")

    # 4. 📚  完整播放清單 (始終顯示)
    lines.append(f"📚  完整播放清單：https://www.youtube.com/playlist?list={playlist_id}")

    return "\n".join(lines)


class StateStore:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self.data: dict[str, Any] = {"videos": {}}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
            self.data.setdefault("videos", {})
        except Exception:
            self.data = {"videos": {}}

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def video(self, video_id: str) -> dict[str, Any]:
        return self.data.setdefault("videos", {}).setdefault(video_id, {})

    def mark(self, video_id: str, key: str, value: Any) -> None:
        record = self.video(video_id)
        record[key] = value
        record["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()


# Re-export the concrete Studio client for existing backfill imports.
from .youtube_studio_client import StudioPrivateClient
