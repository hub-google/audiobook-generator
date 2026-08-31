"""Local GUI for backfilling YouTube info cards and pinned navigation comments.

This tool is intentionally NOT wired into the production upload pipeline.

Flow:
1. Read owned playlists/videos through YouTube Data API v3.
2. Let the operator pick one playlist; the first playlist item is treated as Part 1.
3. For every selected video, write/update an info card through YouTube Studio's
   private edit_video endpoint. The playlist card opens the selected playlist
   starting from Part 1.
4. Post one navigation comment through the official Data API containing both
   the Part 1 URL and playlist URL.
5. Pin that comment with a local Chrome/Selenium session.

Secrets never leave the local machine except in requests directly to Google /
YouTube. This module does not depend on the third-party youtube-studio package.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests
import tkinter as tk
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tkinter import messagebox, ttk

CHANNEL_ID = "UCIUtGUZ24fMsfzZtydQTsPg"
DATA_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
STUDIO_ORIGIN = "https://studio.youtube.com"
STUDIO_EDIT_ENDPOINT = f"{STUDIO_ORIGIN}/youtubei/v1/video_editor/edit_video"
STATE_PATH = Path(__file__).with_name("youtube_backfill_state.json")
CHROME_PROFILE_PATH = Path(__file__).with_name("youtube_backfill_chrome_profile")
COMMENT_MARKER = "【小說導流】"


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


class YouTubeDataClient:
    def __init__(self, slot: int) -> None:
        self.slot = slot
        self.creds = self._credentials(slot)
        self.service = build("youtube", "v3", credentials=self.creds, cache_discovery=False)
        self._assert_channel_owner()

    @staticmethod
    def available_slots() -> list[int]:
        found: list[int] = []
        for slot in range(1, 11):
            if all(
                os.getenv(f"YOUTUBE_{name}_{slot}", "").strip()
                for name in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
            ):
                found.append(slot)
        return found

    @staticmethod
    def _credentials(slot: int) -> Credentials:
        client_id = os.getenv(f"YOUTUBE_CLIENT_ID_{slot}", "").strip()
        client_secret = os.getenv(f"YOUTUBE_CLIENT_SECRET_{slot}", "").strip()
        refresh_token = os.getenv(f"YOUTUBE_REFRESH_TOKEN_{slot}", "").strip()
        if not all((client_id, client_secret, refresh_token)):
            raise RuntimeError(f"OAuth slot {slot} 不完整，請檢查 .env 或環境變數。")
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=DATA_SCOPES,
        )
        creds.refresh(Request())
        return creds

    def _assert_channel_owner(self) -> None:
        result = self.service.channels().list(part="id,snippet", mine=True).execute()
        mine = {item["id"] for item in result.get("items", [])}
        if CHANNEL_ID not in mine:
            actual = ", ".join(sorted(mine)) or "(none)"
            raise RuntimeError(
                f"OAuth slot {self.slot} 不是目標頻道擁有者。mine={actual}"
            )

    def list_playlists(self) -> list[PlaylistRow]:
        rows: list[PlaylistRow] = []
        page_token: str | None = None
        while True:
            payload = self.service.playlists().list(
                part="snippet,contentDetails",
                mine=True,
                maxResults=50,
                pageToken=page_token,
            ).execute()
            for item in payload.get("items", []):
                snippet = item.get("snippet", {})
                rows.append(
                    PlaylistRow(
                        playlist_id=item["id"],
                        title=snippet.get("title", ""),
                        item_count=int(item.get("contentDetails", {}).get("itemCount", 0)),
                        published_at=snippet.get("publishedAt", ""),
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        rows.sort(key=lambda row: row.published_at, reverse=True)
        return rows

    def list_playlist_videos(self, playlist_id: str) -> list[VideoRow]:
        rows: list[VideoRow] = []
        page_token: str | None = None
        while True:
            payload = self.service.playlistItems().list(
                part="snippet",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=page_token,
            ).execute()
            for item in payload.get("items", []):
                snippet = item.get("snippet", {})
                video_id = snippet.get("resourceId", {}).get("videoId")
                if not video_id:
                    continue
                rows.append(
                    VideoRow(
                        video_id=video_id,
                        title=snippet.get("title", ""),
                        position=int(snippet.get("position", len(rows))),
                    )
                )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        rows.sort(key=lambda row: row.position)
        return rows

    def find_existing_nav_comment(self, video_id: str) -> tuple[str, str] | None:
        page_token: str | None = None
        while True:
            try:
                payload = self.service.commentThreads().list(
                    part="snippet",
                    videoId=video_id,
                    maxResults=100,
                    order="time",
                    pageToken=page_token,
                    textFormat="plainText",
                ).execute()
            except HttpError as exc:
                if getattr(exc, "resp", None) is not None and exc.resp.status in (403, 404):
                    return None
                raise
            for item in payload.get("items", []):
                top = item.get("snippet", {}).get("topLevelComment", {})
                snippet = top.get("snippet", {})
                text = snippet.get("textDisplay", "")
                author_channel = snippet.get("authorChannelId", {}).get("value")
                if COMMENT_MARKER in text and author_channel == CHANNEL_ID:
                    return top.get("id", ""), text
            page_token = payload.get("nextPageToken")
            if not page_token:
                return None

    def post_navigation_comment(
        self, video_id: str, first_video_id: str, playlist_id: str
    ) -> str:
        first_url = f"https://youtu.be/{first_video_id}"
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        text = (
            f"{COMMENT_MARKER}\n"
            "🎧 第一次收聽這部小說？建議從第一集開始：\n"
            f"▶ 第一集：{first_url}\n\n"
            "📚 完整播放清單：\n"
            f"{playlist_url}"
        )
        result = self.service.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {"snippet": {"textOriginal": text}},
                }
            },
        ).execute()
        return result["snippet"]["topLevelComment"]["id"]


class StudioPrivateClient:
    """Minimal local implementation of the Studio edit_video request.

    This intentionally copies only the required request shape instead of
    importing the third-party `youtube-studio` npm package.
    """

    def __init__(self, raw_cookie: str) -> None:
        self.raw_cookie = raw_cookie.strip()
        self.cookies = self._parse_cookie(self.raw_cookie)
        self.session = requests.Session()
        self.session.cookies.update(self.cookies)
        self.config: dict[str, str] = {}
        self._bootstrap()

    @staticmethod
    def _parse_cookie(raw: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for chunk in raw.split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    @staticmethod
    def _config_value(text: str, name: str) -> str:
        match = re.search(rf'"{re.escape(name)}"\s*:\s*(?:"([^"]*)"|(\d+))', text)
        if not match:
            return ""
        return match.group(1) or match.group(2) or ""

    def _bootstrap(self) -> None:
        if not self.raw_cookie:
            raise RuntimeError("尚未提供 YouTube Studio Cookie。")
        if not (self.cookies.get("SAPISID") or self.cookies.get("__Secure-3PAPISID")):
            raise RuntimeError("Cookie 缺少 SAPISID / __Secure-3PAPISID。")

        response = self.session.get(
            f"{STUDIO_ORIGIN}/channel/{CHANNEL_ID}", timeout=30
        )
        response.raise_for_status()
        text = response.text
        datasync = self._config_value(text, "DATASYNC_ID")
        sync_first, sep, sync_second = datasync.partition("||")
        self.config = {
            "api_key": self._config_value(text, "INNERTUBE_API_KEY"),
            "client_version": self._config_value(text, "INNERTUBE_CLIENT_VERSION")
            or self._config_value(text, "INNERTUBE_CONTEXT_CLIENT_VERSION")
            or "1.20260826.03.00",
            "auth_user": self._config_value(text, "SESSION_INDEX") or "0",
            "page_id": self._config_value(text, "DELEGATED_SESSION_ID")
            or (sync_first if sep and sync_second else ""),
            "identity_token": self._config_value(text, "ID_TOKEN"),
            "visitor_data": self._config_value(text, "VISITOR_DATA")
            or self._config_value(text, "visitorData"),
            "user_session_id": self._config_value(text, "USER_SESSION_ID")
            or (sync_second if sep and sync_second else sync_first),
        }
        if not self.config["api_key"]:
            raise RuntimeError("無法從 YouTube Studio 取得 INNERTUBE_API_KEY，Cookie 可能已失效。")

    def _authorization(self) -> str:
        timestamp = str(int(time.time()))
        user_session_id = self.config.get("user_session_id", "")
        prefix = f"{user_session_id} " if user_session_id else ""
        suffix = "_u" if user_session_id else ""
        values: list[str] = []
        for scheme, cookie_name in (
            ("SAPISIDHASH", "SAPISID"),
            ("SAPISID1PHASH", "__Secure-1PAPISID"),
            ("SAPISID3PHASH", "__Secure-3PAPISID"),
        ):
            sid = self.cookies.get(cookie_name)
            if not sid:
                continue
            digest = hashlib.sha1(
                f"{prefix}{timestamp} {sid} {STUDIO_ORIGIN}".encode("utf-8")
            ).hexdigest()
            values.append(f"{scheme} {timestamp}_{digest}{suffix}")
        return " ".join(values)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self._authorization(),
            "Origin": STUDIO_ORIGIN,
            "Referer": f"{STUDIO_ORIGIN}/channel/{CHANNEL_ID}",
            "X-Origin": STUDIO_ORIGIN,
            "X-Goog-AuthUser": self.config.get("auth_user", "0"),
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"
            ),
        }
        if self.config.get("page_id"):
            headers["X-Goog-PageId"] = self.config["page_id"]
        if self.config.get("identity_token"):
            headers["X-Youtube-Identity-Token"] = self.config["identity_token"]
        if self.config.get("visitor_data"):
            headers["X-Goog-Visitor-Id"] = self.config["visitor_data"]
        return headers

    def set_navigation_card(
        self, video_id: str, playlist_id: str, first_video_id: str, start_ms: int = 10000
    ) -> None:
        # A playlist card can start at an explicitly chosen video. This lets one
        # card satisfy both goals: enter the complete playlist AND start at Part 1.
        card = {
            "videoId": video_id,
            "teaserStartMs": start_ms,
            "playlistInfoCard": {
                "fullPlaylistId": playlist_id,
                "startVideoId": first_video_id,
            },
            "infoCardEntityId": str(int(time.time() * 1000)),
            "customMessage": "第一次收聽？從第一集開始",
            "teaserText": "第一集＋完整播放清單",
        }
        delegation_context = {
            "roleType": {"channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"},
            "externalChannelId": CHANNEL_ID,
        }
        payload = {
            "context": {
                "client": {
                    "clientName": 62,
                    "clientVersion": self.config["client_version"],
                    "hl": "zh-TW",
                    "gl": "TW",
                },
                "request": {"returnLogEntry": True, "internalExperimentFlags": []},
                "user": {
                    **(
                        {"onBehalfOfUser": self.config["page_id"]}
                        if self.config.get("page_id")
                        else {}
                    ),
                    "delegationContext": delegation_context,
                    "serializedDelegationContext": "",
                },
            },
            "delegationContext": delegation_context,
            "externalVideoId": video_id,
            "infoCardEdit": {"infoCards": [card]},
        }
        response = self.session.post(
            STUDIO_EDIT_ENDPOINT,
            params={"alt": "json", "key": self.config["api_key"]},
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Studio info card HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Studio info card 回傳不是 JSON。") from exc
        if body.get("error"):
            raise RuntimeError(f"Studio info card API error: {body['error']}")
        extension = body.get("responseContext", {}).get("webResponseContextExtensionData", {})
        if extension.get("challenge"):
            raise RuntimeError("Studio 要求額外登入驗證，請更新 Cookie。")


class PinBrowser:
    """Pin a known channel-owner comment through local Chrome UI.

    We deliberately keep this local and visible. YouTube does not expose a
    stable public pin-comment endpoint; using the UI also avoids hard-coding an
    opaque/private comment-action protobuf that changes frequently.
    """

    def __init__(self, status: Callable[[str], None]) -> None:
        self.status = status
        self.driver = None

    def _ensure_driver(self):
        if self.driver is not None:
            return self.driver
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError as exc:
            raise RuntimeError("缺少 selenium；請先執行 pip install selenium") from exc
        options = Options()
        options.add_argument(f"--user-data-dir={CHROME_PROFILE_PATH.resolve()}")
        options.add_argument("--lang=zh-TW")
        options.add_argument("--window-size=1280,900")
        self.driver = webdriver.Chrome(options=options)
        return self.driver

    def ensure_logged_in(self) -> None:
        driver = self._ensure_driver()
        driver.get("https://www.youtube.com/")
        time.sleep(2)
        self.status("Chrome 已開啟。第一次使用請先在這個視窗登入目標 YouTube 帳號。")

    def pin_comment(self, video_id: str, comment_id: str) -> None:
        driver = self._ensure_driver()
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise RuntimeError("缺少 selenium") from exc

        driver.get(f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}")
        wait = WebDriverWait(driver, 25)
        # Scroll so comments hydrate.
        driver.execute_script("window.scrollTo(0, 900);")
        time.sleep(2)

        comment = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, f"ytd-comment-thread-renderer #comment")
            )
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", comment)
        time.sleep(1)

        # Prefer the thread selected by the `lc` URL. If YouTube changes the
        # highlighted markup, fall back to the first owner navigation comment.
        threads = driver.find_elements(By.CSS_SELECTOR, "ytd-comment-thread-renderer")
        target = None
        for thread in threads:
            try:
                text = thread.find_element(By.CSS_SELECTOR, "#content-text").text
            except Exception:
                continue
            if COMMENT_MARKER in text:
                target = thread
                break
        if target is None and threads:
            target = threads[0]
        if target is None:
            raise RuntimeError("找不到剛發布的留言。")

        menu = target.find_element(By.CSS_SELECTOR, "#action-menu button, #menu button")
        driver.execute_script("arguments[0].click();", menu)
        menu_item = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//ytd-menu-service-item-renderer//*[contains(normalize-space(.),'置頂') or contains(normalize-space(.),'Pin')]",
                )
            )
        )
        driver.execute_script("arguments[0].click();", menu_item)
        time.sleep(0.8)

        # Confirmation dialog appears if another comment is already pinned.
        confirm_buttons = driver.find_elements(
            By.XPATH,
            "//yt-confirm-dialog-renderer//button//*[contains(normalize-space(.),'置頂') or contains(normalize-space(.),'PIN') or contains(normalize-space(.),'Pin')]/ancestor::button",
        )
        if confirm_buttons:
            driver.execute_script("arguments[0].click();", confirm_buttons[-1])
        time.sleep(1.2)

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        load_dotenv()
        self.title("YouTube 小說資訊卡／置頂留言補登工具")
        self.geometry("1120x760")
        self.minsize(920, 640)
        self.data_client: YouTubeDataClient | None = None
        self.studio_client: StudioPrivateClient | None = None
        self.pin_browser: PinBrowser | None = None
        self.state_store = StateStore()
        self.playlists: list[PlaylistRow] = []
        self.videos: list[VideoRow] = []
        self.event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self._build_ui()
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x")
        ttk.Label(top, text=f"目標頻道：{CHANNEL_ID}").pack(side="left")
        ttk.Label(top, text="OAuth Slot").pack(side="left", padx=(20, 5))
        slots = [str(x) for x in YouTubeDataClient.available_slots()] or ["1"]
        self.slot_var = tk.StringVar(value=slots[0])
        ttk.Combobox(top, width=5, textvariable=self.slot_var, values=slots, state="readonly").pack(side="left")
        ttk.Button(top, text="讀取播放清單", command=self.load_playlists).pack(side="left", padx=8)
        ttk.Button(top, text="開啟登入 Chrome", command=self.open_pin_browser).pack(side="left", padx=8)

        auth_box = ttk.LabelFrame(root, text="YouTube Studio Cookie（只存在記憶體，不會寫入 repo/state）", padding=8)
        auth_box.pack(fill="x", pady=(10, 8))
        self.cookie_text = tk.Text(auth_box, height=3, wrap="word")
        self.cookie_text.pack(fill="x")
        if os.getenv("YOUTUBE_STUDIO_COOKIES"):
            self.cookie_text.insert("1.0", os.getenv("YOUTUBE_STUDIO_COOKIES", ""))

        split = ttk.Panedwindow(root, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=2)

        ttk.Label(left, text="播放清單").pack(anchor="w")
        self.playlist_tree = ttk.Treeview(
            left, columns=("count", "id"), show="tree headings", height=18
        )
        self.playlist_tree.heading("#0", text="名稱")
        self.playlist_tree.heading("count", text="影片")
        self.playlist_tree.heading("id", text="Playlist ID")
        self.playlist_tree.column("#0", width=260)
        self.playlist_tree.column("count", width=55, anchor="center")
        self.playlist_tree.column("id", width=150)
        self.playlist_tree.pack(fill="both", expand=True)
        self.playlist_tree.bind("<<TreeviewSelect>>", self._playlist_selected)

        info = ttk.LabelFrame(right, text="選取播放清單", padding=8)
        info.pack(fill="x")
        self.selection_text = tk.StringVar(value="尚未選擇")
        ttk.Label(info, textvariable=self.selection_text, justify="left").pack(anchor="w")

        options = ttk.Frame(right)
        options.pack(fill="x", pady=8)
        self.do_card = tk.BooleanVar(value=True)
        self.do_comment = tk.BooleanVar(value=True)
        self.do_pin = tk.BooleanVar(value=True)
        self.skip_done = tk.BooleanVar(value=True)
        self.include_first = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="資訊卡", variable=self.do_card).pack(side="left")
        ttk.Checkbutton(options, text="導流留言", variable=self.do_comment).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="置頂留言", variable=self.do_pin).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="跳過已完成", variable=self.skip_done).pack(side="left", padx=8)
        ttk.Checkbutton(options, text="包含第一集", variable=self.include_first).pack(side="left", padx=8)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="開始批次補登", command=self.start_batch)
        self.start_button.pack(side="left")
        ttk.Button(actions, text="停止", command=self.stop_batch).pack(side="left", padx=8)

        self.video_tree = ttk.Treeview(
            right, columns=("pos", "video", "card", "comment", "pin"), show="headings", height=18
        )
        for col, title, width in (
            ("pos", "集", 45),
            ("video", "影片", 360),
            ("card", "資訊卡", 75),
            ("comment", "留言", 75),
            ("pin", "置頂", 75),
        ):
            self.video_tree.heading(col, text=title)
            self.video_tree.column(col, width=width, anchor="w" if col == "video" else "center")
        self.video_tree.pack(fill="both", expand=True)

        log_box = ttk.LabelFrame(root, text="執行紀錄", padding=6)
        log_box.pack(fill="both", pady=(8, 0))
        self.log_text = tk.Text(log_box, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _run_thread(self, func: Callable[[], None]) -> None:
        threading.Thread(target=func, daemon=True).start()

    def load_playlists(self) -> None:
        def work() -> None:
            try:
                self.event_queue.put(("log", "正在驗證 OAuth 並讀取播放清單…"))
                client = YouTubeDataClient(int(self.slot_var.get()))
                playlists = client.list_playlists()
                self.event_queue.put(("playlists", (client, playlists)))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
        self._run_thread(work)

    def _playlist_selected(self, _event=None) -> None:
        selection = self.playlist_tree.selection()
        if not selection or not self.data_client:
            return
        index = int(selection[0])
        row = self.playlists[index]

        def work() -> None:
            try:
                videos = self.data_client.list_playlist_videos(row.playlist_id)
                self.event_queue.put(("videos", (row, videos)))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
        self._run_thread(work)

    def open_pin_browser(self) -> None:
        def work() -> None:
            try:
                if self.pin_browser is None:
                    self.pin_browser = PinBrowser(lambda msg: self.event_queue.put(("log", msg)))
                self.pin_browser.ensure_logged_in()
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
        self._run_thread(work)

    def start_batch(self) -> None:
        if not self.data_client or not self.videos:
            messagebox.showwarning("尚未選擇", "請先讀取並選擇播放清單。")
            return
        raw_cookie = self.cookie_text.get("1.0", "end").strip()
        if self.do_card.get() and not raw_cookie:
            messagebox.showwarning("缺少 Cookie", "要補資訊卡時必須貼入 YouTube Studio Cookie。")
            return
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        playlist = self.playlists[int(self.playlist_tree.selection()[0])]
        videos = list(self.videos)
        first = videos[0]

        def work() -> None:
            try:
                studio = StudioPrivateClient(raw_cookie) if self.do_card.get() else None
                self.studio_client = studio
                if self.do_pin.get() and self.pin_browser is None:
                    self.pin_browser = PinBrowser(lambda msg: self.event_queue.put(("log", msg)))
                    self.pin_browser.ensure_logged_in()

                for video in videos:
                    if self.stop_event.is_set():
                        break
                    if video.position == 0 and not self.include_first.get():
                        continue
                    record = self.state_store.video(video.video_id)
                    self.event_queue.put(("log", f"處理 {video.position + 1}: {video.title}"))

                    if self.do_card.get():
                        if self.skip_done.get() and record.get("card_playlist_id") == playlist.playlist_id:
                            self.event_queue.put(("status", (video.video_id, "card", "已完成")))
                        else:
                            studio.set_navigation_card(
                                video.video_id, playlist.playlist_id, first.video_id
                            )
                            self.state_store.mark(video.video_id, "card_playlist_id", playlist.playlist_id)
                            self.event_queue.put(("status", (video.video_id, "card", "OK")))

                    comment_id = record.get("comment_id", "")
                    if self.do_comment.get() or self.do_pin.get():
                        if not comment_id:
                            existing = self.data_client.find_existing_nav_comment(video.video_id)
                            if existing:
                                comment_id = existing[0]
                                self.state_store.mark(video.video_id, "comment_id", comment_id)
                                self.event_queue.put(("status", (video.video_id, "comment", "已有")))
                            elif self.do_comment.get():
                                comment_id = self.data_client.post_navigation_comment(
                                    video.video_id, first.video_id, playlist.playlist_id
                                )
                                self.state_store.mark(video.video_id, "comment_id", comment_id)
                                self.event_queue.put(("status", (video.video_id, "comment", "OK")))
                            else:
                                raise RuntimeError("找不到既有導流留言，且已取消『導流留言』選項。")
                        else:
                            self.event_queue.put(("status", (video.video_id, "comment", "已完成")))

                    if self.do_pin.get():
                        if self.skip_done.get() and record.get("pinned_comment_id") == comment_id:
                            self.event_queue.put(("status", (video.video_id, "pin", "已完成")))
                        else:
                            assert self.pin_browser is not None
                            self.pin_browser.pin_comment(video.video_id, comment_id)
                            self.state_store.mark(video.video_id, "pinned_comment_id", comment_id)
                            self.event_queue.put(("status", (video.video_id, "pin", "OK")))

                self.event_queue.put(("done", "批次處理完成。" if not self.stop_event.is_set() else "已停止。"))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
                self.event_queue.put(("done", "批次處理中止。"))

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
                    self.log(f"ERROR: {payload}")
                    messagebox.showerror("錯誤", str(payload))
                elif kind == "playlists":
                    self.data_client, self.playlists = payload
                    self.playlist_tree.delete(*self.playlist_tree.get_children())
                    for idx, row in enumerate(self.playlists):
                        self.playlist_tree.insert(
                            "", "end", iid=str(idx), text=row.title,
                            values=(row.item_count, row.playlist_id)
                        )
                    self.log(f"已載入 {len(self.playlists)} 個播放清單。")
                elif kind == "videos":
                    row, self.videos = payload
                    self.video_tree.delete(*self.video_tree.get_children())
                    for idx, video in enumerate(self.videos):
                        record = self.state_store.video(video.video_id)
                        self.video_tree.insert(
                            "", "end", iid=str(idx), text=video.video_id,
                            values=(
                                video.position + 1,
                                video.title,
                                "已完成" if record.get("card_playlist_id") == row.playlist_id else "",
                                "已完成" if record.get("comment_id") else "",
                                "已完成" if record.get("pinned_comment_id") else "",
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
                elif kind == "status":
                    video_id, field, value = payload
                    self._update_status(video_id, field, value)
                elif kind == "done":
                    self.log(str(payload))
                    self.start_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def destroy(self) -> None:
        if self.pin_browser is not None:
            self.pin_browser.close()
        super().destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
