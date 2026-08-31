"""Local GUI for backfilling YouTube info cards and pinned navigation comments.

This tool is intentionally NOT wired into the production upload pipeline.

Flow:
1. Read owned playlists/videos through the authenticated YouTube Studio session.
2. Let the operator pick one playlist; the first playlist item is treated as Part 1.
3. For every selected video, write/update an info card through YouTube Studio's
   private edit_video endpoint. The playlist card opens the selected playlist
   starting from Part 1.
4. Post one navigation comment through YouTube's authenticated web endpoint containing both
   the Part 1 URL and playlist URL.
5. Pin that comment with the opaque action token returned by YouTube; browser
   automation is not used.

Secrets never leave the local machine except in requests directly to Google /
YouTube. This module does not depend on the third-party youtube-studio package.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Double-clicking this file may use the system Python, which does not have this
# project's packages. Relaunch through the prepared virtual environment first.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VENV_PYTHON = _PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
if __name__ == "__main__" and _VENV_PYTHON.is_file() and Path(sys.executable).resolve() != _VENV_PYTHON.resolve():
    subprocess.Popen(
        [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(_PROJECT_ROOT),
    )
    raise SystemExit

import requests
import tkinter as tk
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from tkinter import messagebox, ttk

CHANNEL_ID = "UCIUtGUZ24fMsfzZtydQTsPg"
STUDIO_ORIGIN = "https://studio.youtube.com"
STUDIO_EDIT_ENDPOINT = f"{STUDIO_ORIGIN}/youtubei/v1/video_editor/edit_video"
STATE_PATH = Path(__file__).with_name("youtube_backfill_state.json")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMMENT_MARKER = "【小說導流】"
CARD_FIRST_EP_START_MS = 3000   # 0:03 跳出第一集連結
CARD_PLAYLIST_START_MS = 14000  # 0:14 第一集收合後跳出完整播放清單
CARD_STATE_VERSION = 3          # 升級到 version 3，自動重新替換舊資訊卡
ENDSCREEN_STATE_VERSION = 1     # 片尾播放清單結束畫面版本
CHROME_BINARY = Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe"
CHROME_PROFILE = Path(__file__).with_name("youtube_backfill_chrome_profile")


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


class StudioPrivateClient:
    """Minimal local implementation of the Studio edit_video request.

    This intentionally copies only the required request shape instead of
    importing the third-party `youtube-studio` npm package.
    """

    def __init__(self, raw_cookie: str, channel_id: str = CHANNEL_ID) -> None:
        self.channel_id = channel_id
        self.raw_cookie = self._normalize_cookie(raw_cookie)
        self.cookies = self._parse_cookie(self.raw_cookie)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        })
        self.config: dict[str, str] = {}
        self._bootstrap()

    @staticmethod
    def _normalize_cookie(raw: str) -> str:
        """Accept a raw Cookie header as well as JSON or chat/Markdown-escaped text."""
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        value = re.sub(r"^\s*cookie\s*:\s*", "", value, flags=re.IGNORECASE)
        # Handle JSON array format from Cookie Editor extensions
        if value.startswith("[") and value.endswith("]"):
            try:
                items = json.loads(value)
                if isinstance(items, list):
                    cookie_pairs = []
                    for item in items:
                        if isinstance(item, dict) and "name" in item and "value" in item:
                            cookie_pairs.append(f"{item['name']}={item['value']}")
                    if cookie_pairs:
                        return "; ".join(cookie_pairs)
            except Exception:
                pass
        # Handle Netscape cookie format (tab/space separated)
        if "\t" in value:
            pairs = []
            for line in value.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
            if pairs:
                return "; ".join(pairs)
        # Chat clients commonly escape underscores and asterisks in pasted text.
        value = re.sub(r"\\([_*])", r"\1", value)
        value = re.sub(r"[\r\n]+", " ", value)
        return value.strip()

    @staticmethod
    def _parse_cookie(raw: str) -> dict[str, str]:
        trimmed = raw.strip()
        if trimmed.startswith("[") and trimmed.endswith("]"):
            try:
                items = json.loads(trimmed)
                if isinstance(items, list):
                    result = {
                        str(item["name"]).strip(): str(item["value"]).strip()
                        for item in items
                        if isinstance(item, dict) and "name" in item and "value" in item
                    }
                    if result:
                        return result
            except Exception:
                pass
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
        sid = (
            self.cookies.get("SAPISID")
            or self.cookies.get("__Secure-3PAPISID")
            or self.cookies.get("__Secure-1PAPISID")
        )
        if not sid:
            raise RuntimeError("Cookie 缺少 SAPISID / __Secure-3PAPISID。請確認已登入 YouTube 並複製完整 Cookie。")

        studio_url = f"{STUDIO_ORIGIN}/"
        init_auth = self._authorization(STUDIO_ORIGIN)
        init_headers = {
            "Cookie": self.raw_cookie,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        }
        if init_auth:
            init_headers["Authorization"] = init_auth
            init_headers["Origin"] = STUDIO_ORIGIN
            init_headers["X-Origin"] = STUDIO_ORIGIN

        studio_text = ""
        try:
            response = self.session.get(
                studio_url,
                headers=init_headers,
                timeout=30,
                allow_redirects=True,
            )
            if response.ok and response.url.startswith(STUDIO_ORIGIN):
                studio_text = response.text
                channel_match = re.search(r"/channel/(UC[\w-]+)", response.url)
                if channel_match:
                    self.channel_id = channel_match.group(1)
        except Exception:
            pass

        # Extract structured ytcfg if available
        ytcfg_data: dict[str, Any] = {}
        if studio_text:
            matches = re.findall(r'ytcfg\.set\s*\(\s*({.+?})\s*\);', studio_text, re.DOTALL)
            for m in matches:
                try:
                    ytcfg_data.update(json.loads(m))
                except Exception:
                    pass

        # Fetch fallback from main YouTube page if needed
        fallback_text = ""
        if not ytcfg_data.get("INNERTUBE_API_KEY"):
            try:
                fallback = self.session.get(
                    "https://www.youtube.com/",
                    headers={"Cookie": self.raw_cookie},
                    timeout=30,
                )
                if fallback.ok:
                    fallback_text = fallback.text
                    matches = re.findall(r'ytcfg\.set\s*\(\s*({.+?})\s*\);', fallback_text, re.DOTALL)
                    for m in matches:
                        try:
                            ytcfg_data.update(json.loads(m))
                        except Exception:
                            pass
            except Exception:
                pass

        combined_text = f"{studio_text}\n{fallback_text}"
        detected_channel = (
            ytcfg_data.get("CHANNEL_ID")
            or self._config_value(studio_text, "CHANNEL_ID")
            or self._config_value(studio_text, "externalChannelId")
            or self._config_value(combined_text, "CHANNEL_ID")
        )
        if detected_channel:
            self.channel_id = detected_channel

        self.config = {
            "api_key": (
                ytcfg_data.get("INNERTUBE_API_KEY")
                or self._config_value(studio_text, "INNERTUBE_API_KEY")
                or self._config_value(fallback_text, "INNERTUBE_API_KEY")
                or "AIzaSyBUPetSUmoZL-OhlxA7wSac5XinrygCqMo"
            ),
            "client_version": (
                ytcfg_data.get("INNERTUBE_CLIENT_VERSION")
                or self._config_value(studio_text, "INNERTUBE_CLIENT_VERSION")
                or self._config_value(studio_text, "INNERTUBE_CONTEXT_CLIENT_VERSION")
                or "1.20260829.00.00"
            ),
            "web_client_version": (
                self._config_value(fallback_text, "INNERTUBE_CLIENT_VERSION")
                or self._config_value(fallback_text, "INNERTUBE_CONTEXT_CLIENT_VERSION")
                or "2.20260828.01.00"
            ),
            "auth_user": str(
                ytcfg_data.get("SESSION_INDEX")
                or self._config_value(studio_text, "SESSION_INDEX")
                or self._config_value(combined_text, "SESSION_INDEX")
                or "0"
            ),
            "page_id": (
                ytcfg_data.get("DELEGATED_SESSION_ID")
                or self._config_value(studio_text, "DELEGATED_SESSION_ID")
                or self._config_value(combined_text, "DELEGATED_SESSION_ID")
                or ""
            ),
            "identity_token": (
                ytcfg_data.get("ID_TOKEN")
                or self._config_value(studio_text, "ID_TOKEN")
                or self._config_value(combined_text, "ID_TOKEN")
                or ""
            ),
            "visitor_data": (
                ytcfg_data.get("VISITOR_DATA")
                or self._config_value(studio_text, "VISITOR_DATA")
                or self._config_value(studio_text, "visitorData")
                or self._config_value(combined_text, "VISITOR_DATA")
                or ""
            ),
            "user_session_id": str(
                ytcfg_data.get("USER_SESSION_ID")
                or self._config_value(studio_text, "USER_SESSION_ID")
                or ""
            ),
            "delegation_serialized": (
                ytcfg_data.get("INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT")
                or self._config_value(studio_text, "INNERTUBE_CONTEXT_SERIALIZED_DELEGATION_CONTEXT")
                or ""
            ),
        }

    def _authorization(self, origin: str = STUDIO_ORIGIN) -> str:
        sid = (
            self.cookies.get("SAPISID")
            or self.cookies.get("__Secure-3PAPISID")
            or self.cookies.get("__Secure-1PAPISID")
        )
        if not sid:
            return ""
        timestamp = str(int(time.time()))
        user_session_id = self.config.get("user_session_id", "")
        prefix = f"{user_session_id} " if user_session_id else ""
        suffix = "_u" if user_session_id else ""
        digest = hashlib.sha1(
            f"{prefix}{timestamp} {sid} {origin}".encode("utf-8")
        ).hexdigest()
        return f"SAPISIDHASH {timestamp}_{digest}{suffix}"

    def _headers(
        self, origin: str = STUDIO_ORIGIN, referer: str | None = None
    ) -> dict[str, str]:
        auth_header = self._authorization(origin)
        headers = {
            "Origin": origin,
            "Referer": referer or f"{origin}/",
            "X-Origin": origin,
            "X-Goog-AuthUser": self.config.get("auth_user", "0"),
            "Content-Type": "application/json",
            "Cookie": self.raw_cookie,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if auth_header:
            headers["Authorization"] = auth_header
        if self.config.get("page_id"):
            headers["X-Goog-PageId"] = self.config["page_id"]
        if self.config.get("identity_token"):
            headers["X-Youtube-Identity-Token"] = self.config["identity_token"]
        if self.config.get("visitor_data"):
            headers["X-Goog-Visitor-Id"] = self.config["visitor_data"]
        return headers

    def _context(self) -> dict[str, Any]:
        user_dict: dict[str, Any] = {
            "serializedDelegationContext": self.config.get("delegation_serialized", ""),
        }
        if self.channel_id:
            user_dict["delegationContext"] = {
                "roleType": {"channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"},
                "externalChannelId": self.channel_id,
            }
        if self.config.get("page_id"):
            user_dict["onBehalfOfUser"] = self.config["page_id"]

        return {
            "client": {
                "clientName": 62,
                "clientVersion": self.config.get("client_version", "1.20260829.00.00"),
                "hl": "zh-TW",
                "gl": "TW",
            },
            "request": {"returnLogEntry": True, "internalExperimentFlags": []},
            "user": user_dict,
        }

    def _web_context(self) -> dict[str, Any]:
        context = self._context()
        context["client"] = {
            "clientName": 1,
            "clientVersion": self.config.get("web_client_version", "2.20260828.01.00"),
            "hl": "zh-TW",
            "gl": "TW",
        }
        return context

    def _studio_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{STUDIO_ORIGIN}/youtubei/v1/{path}",
            params={"alt": "json", "key": self.config["api_key"]},
            headers=self._headers(STUDIO_ORIGIN, f"{STUDIO_ORIGIN}/"),
            json=payload,
            timeout=30,
        )
        if not response.ok:
            if response.status_code == 401:
                raise RuntimeError(
                    f"Studio {path} 驗證失敗 (HTTP 401)。\n"
                    "可能原因：Cookie 已過期或不完整，請從已登入的 YouTube Studio 重新複製最新 Cookie。\n"
                    f"詳細回應：{response.text[:300]}"
                )
            raise RuntimeError(f"Studio {path} HTTP {response.status_code}: {response.text[:500]}")
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"Studio {path} error: {body['error']}")
        return body

    def _youtube_post(
        self, path: str, payload: dict[str, Any], video_id: str = ""
    ) -> dict[str, Any]:
        origin = "https://www.youtube.com"
        referer = f"{origin}/watch?v={video_id}" if video_id else f"{origin}/"
        response = self.session.post(
            f"{origin}/youtubei/v1/{path}",
            params={"prettyPrint": "false", "key": self.config["api_key"]},
            headers=self._headers(origin, referer),
            json=payload,
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"YouTube {path} HTTP {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"YouTube {path} error: {body['error']}")
        return body

    @staticmethod
    def _walk(value: Any):
        yield value
        if isinstance(value, dict):
            for child in value.values():
                yield from StudioPrivateClient._walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from StudioPrivateClient._walk(child)

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return ""
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        return "".join(str(x.get("text", "")) for x in value.get("runs", []) if isinstance(x, dict))

    def list_playlists(self) -> list[PlaylistRow]:
        found: dict[str, PlaylistRow] = {}
        page_token = ""
        while True:
            payload: dict[str, Any] = {
                "context": self._context(),
                "mask": {"playlistId": True, "title": True, "videoCount": True},
                "memberVideoIds": [],
                "pageSize": 500,
                "pageToken": page_token,
            }
            if self.channel_id:
                payload["channelId"] = self.channel_id
                payload["delegationContext"] = {
                    "externalChannelId": self.channel_id,
                    "roleType": {
                        "channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"
                    },
                }
            body = self._studio_post("creator/list_creator_playlists", payload)
            # Current Studio responses use the top-level `playlists` array.
            # Keep the recursive fallback for minor response-envelope changes.
            candidates = body.get("playlists")
            nodes = candidates if isinstance(candidates, list) else self._walk(body)
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                playlist_id = node.get("playlistId") or node.get("id")
                if not isinstance(playlist_id, str) or not playlist_id:
                    continue
                title = self._text(node.get("title")) or self._text(node.get("playlistTitle"))
                count = node.get("videoCount", node.get("itemCount"))
                if isinstance(count, dict):
                    count = count.get("value")
                try:
                    count = int(count)
                except (TypeError, ValueError):
                    count = -1
                if title:
                    found[playlist_id] = PlaylistRow(playlist_id, title, count)
            next_token = body.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                break
            page_token = next_token
        if not found:
            keys = ", ".join(sorted(body.keys()))
            raise RuntimeError(
                f"Studio 回應沒有播放清單資料（回應欄位：{keys or '無'}）。"
            )
        return list(found.values())

    def list_playlist_videos(self, playlist_id: str) -> list[VideoRow]:
        # VIDEO_ORDER_PLAYLIST is not a valid Studio enum. Query with Studio's
        # display-time order and then restore chronological episode order below.
        page_token = ""
        raw_rows: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        while True:
            filter_dict: dict[str, Any] = {
                "playlistIdIs": {"value": playlist_id}
            }
            if self.channel_id:
                filter_payload: dict[str, Any] = {"and": {"operands": [
                    {"channelIdIs": {"value": self.channel_id}},
                    filter_dict,
                ]}}
            else:
                filter_payload = filter_dict

            payload: dict[str, Any] = {
                "context": self._context(),
                "pageSize": 500,
                "pageToken": page_token,
                "filter": filter_payload,
                "order": "VIDEO_ORDER_DISPLAY_TIME_DESC",
                "mask": {
                    "videoId": True,
                    "title": True,
                    "titleFormattedString": {"all": True},
                    "timeCreatedSeconds": True,
                    "timePublishedSeconds": True,
                },
            }
            if self.channel_id:
                payload["channelIds"] = [self.channel_id]

            body = self._studio_post("creator/list_creator_videos", payload)
            candidates = body.get("videos")
            nodes = candidates if isinstance(candidates, list) else self._walk(body)
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                video_id = node.get("videoId") or node.get("encryptedVideoId")
                if not isinstance(video_id, str) or len(video_id) != 11 or video_id in seen:
                    continue
                title = self._text(node.get("title")) or self._text(node.get("titleFormattedString"))
                if not title:
                    continue
                timestamp = node.get("timeCreatedSeconds", node.get("timePublishedSeconds", 0))
                try:
                    timestamp = int(timestamp)
                except (TypeError, ValueError):
                    timestamp = 0
                seen.add(video_id)
                raw_rows.append((timestamp, video_id, title))
            next_token = body.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                break
            page_token = next_token

        # These audiobook episodes are uploaded in episode order. Oldest first
        # therefore matches the playlist's Part 1 -> Part N order.
        raw_rows.sort(key=lambda row: (row[0] == 0, row[0], row[2]))
        rows = [
            VideoRow(video_id, title, position)
            for position, (_, video_id, title) in enumerate(raw_rows)
        ]
        if not rows:
            keys = ", ".join(sorted(body.keys()))
            raise RuntimeError(
                f"Studio 回應沒有此播放清單的影片（回應欄位：{keys or '無'}）。"
            )
        return rows

    @staticmethod
    def _create_comment_params(video_id: str) -> str:
        raw = bytes((0x12, len(video_id))) + video_id.encode() + bytes((0x2A, 0, 0x50, 7))
        return base64.b64encode(raw).decode()

    def post_navigation_comment(self, video_id: str, first_video_id: str, playlist_id: str) -> tuple[str, dict[str, Any]]:
        text = (
            f"{COMMENT_MARKER}\n🎧 第一次收聽這部小說？建議從第一集開始：\n"
            f"▶ 第一集：https://youtu.be/{first_video_id}\n\n"
            f"📚 完整播放清單：\nhttps://www.youtube.com/playlist?list={playlist_id}"
        )
        body = self._youtube_post("comment/create_comment", {
            "context": self._web_context(), "commentText": text,
            "createCommentParams": self._create_comment_params(video_id),
        }, video_id)
        for node in self._walk(body):
            if isinstance(node, dict):
                cid = node.get("commentId") or node.get("commentIdString")
                if isinstance(cid, str) and cid:
                    return cid, body
        raise RuntimeError("Studio 留言已送出，但回應中找不到 comment ID，為避免重複留言已中止。")

    @staticmethod
    def _json_assignment(page: str, variable: str) -> dict[str, Any] | None:
        """Decode a JSON object assigned to a JavaScript bootstrap variable."""
        match = re.search(rf"(?:var\s+)?{re.escape(variable)}\s*=\s*", page)
        if not match:
            return None
        start = page.find("{", match.end())
        if start < 0:
            return None
        decoder = json.JSONDecoder()
        try:
            value, _ = decoder.raw_decode(page[start:])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _watch_page_data(self, video_id: str, comment_id: str) -> dict[str, Any] | None:
        response = self.session.get(
            "https://www.youtube.com/watch",
            params={"v": video_id, "lc": comment_id},
            headers=self._headers(
                "https://www.youtube.com",
                f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
            ),
            timeout=30,
        )
        response.raise_for_status()
        return self._json_assignment(response.text, "ytInitialData")

    @classmethod
    def _continuation_tokens(cls, source: Any) -> list[str]:
        tokens: list[str] = []
        for node in cls._walk(source):
            if not isinstance(node, dict):
                continue
            command = node.get("continuationCommand")
            if not isinstance(command, dict):
                continue
            token = command.get("token")
            if isinstance(token, str) and token and token not in tokens:
                tokens.append(token)
        return tokens

    def _existing_comment_pin_action(self, video_id: str, comment_id: str) -> str:
        """Resolve the owner pin action for a comment created by an earlier run."""
        initial = self._watch_page_data(video_id, comment_id)
        action = self._pin_action_from_source(initial, comment_id)
        if action:
            return action

        # Watch pages lazy-load comments. With `lc=comment_id`, the comment
        # continuation resolves to the highlighted thread, but the menu/action
        # token is normally present only in the continuation response.
        pending = self._continuation_tokens(initial)
        seen: set[str] = set()
        requests_made = 0
        while pending and requests_made < 20:
            token = pending.pop(0)
            if token in seen:
                continue
            seen.add(token)
            body = self._youtube_post(
                "next",
                {"context": self._web_context(), "continuation": token},
                video_id,
            )
            requests_made += 1
            action = self._pin_action_from_source(body, comment_id)
            if action:
                return action
            for child_token in self._continuation_tokens(body):
                if child_token not in seen and child_token not in pending:
                    pending.append(child_token)
        return ""

    @classmethod
    def _pin_action_from_source(cls, source: Any, comment_id: str) -> str:
        for container in cls._walk(source):
            if not isinstance(container, dict):
                continue
            serialized = json.dumps(container, ensure_ascii=False).lower()
            if comment_id.lower() not in serialized:
                continue
            if not any(marker in serialized for marker in ('"pinned"', '"pin"', "置頂")):
                continue
            candidates: list[tuple[int, str]] = []
            for node in cls._walk(container):
                if not isinstance(node, dict):
                    continue
                node_text = json.dumps(node, ensure_ascii=False).lower()
                if not any(marker in node_text for marker in ('"pinned"', '"pin"', "置頂")):
                    continue
                for child in cls._walk(node):
                    if not isinstance(child, dict):
                        continue
                    endpoint = child.get("performCommentActionEndpoint")
                    if isinstance(endpoint, dict) and isinstance(endpoint.get("action"), str):
                        candidates.append((len(node_text), endpoint["action"]))
            if candidates:
                return min(candidates)[1]
        return ""

    def pin_comment(
        self,
        video_id: str,
        comment_id: str,
        create_response: dict[str, Any] | None = None,
    ) -> None:
        # The pin token is opaque and is supplied by YouTube with the newly-created
        # comment. Never synthesize it: submit the exact performCommentAction endpoint.
        action = self._pin_action_from_source(create_response, comment_id) if create_response else ""
        if not action:
            # A previous run may already have created the comment. Opening its
            # highlighted-comment URL asks YouTube for the current menu endpoints,
            # including the owner-only pin action, without fabricating opaque tokens.
            action = self._existing_comment_pin_action(video_id, comment_id)
        if not action:
            raise RuntimeError(
                f"已讀取既有導流留言 ID {comment_id}，但 YouTube 留言區"
                "沒有回傳置頂 action token，因此未執行置頂。"
            )
        self._youtube_post("comment/perform_comment_action", {
            "context": self._web_context(), "actions": [action],
        })


    def set_navigation_card(
        self,
        video_id: str,
        first_video_id: str,
        playlist_id: str,
        is_first_episode: bool = False,
    ) -> None:
        cards = []
        now_ms = int(time.time() * 1000)
        if not is_first_episode and first_video_id:
            # Card 1: Episode 1 link @ 0:03 (3000ms)
            cards.append({
                "videoId": video_id,
                "teaserStartMs": CARD_FIRST_EP_START_MS,
                "videoInfoCard": {
                    "fullVideoId": first_video_id,
                },
                "infoCardEntityId": str(now_ms),
                "customMessage": "第一次收聽？建議從第1集開始",
                "teaserText": "👉 點此從【第 1 集】開始聽",
            })
            # Card 2: Playlist link @ 0:14 (14000ms)
            cards.append({
                "videoId": video_id,
                "teaserStartMs": CARD_PLAYLIST_START_MS,
                "playlistInfoCard": {
                    "fullPlaylistId": playlist_id,
                },
                "infoCardEntityId": str(now_ms + 1),
                "customMessage": "完整播放清單",
                "teaserText": "📚 本部小說【完整播放清單】",
            })
        else:
            # Episode 1 itself: Playlist link @ 0:03 (3000ms)
            cards.append({
                "videoId": video_id,
                "teaserStartMs": CARD_FIRST_EP_START_MS,
                "playlistInfoCard": {
                    "fullPlaylistId": playlist_id,
                },
                "infoCardEntityId": str(now_ms),
                "customMessage": "完整播放清單",
                "teaserText": "📚 本部小說【完整播放清單】",
            })

        delegation_context = {
            "roleType": {"channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"},
            "externalChannelId": self.channel_id,
        } if self.channel_id else {}
        payload = {
            "context": self._context(),
            "externalVideoId": video_id,
            "infoCardEdit": {"infoCards": cards},
        }
        if delegation_context:
            payload["delegationContext"] = delegation_context

        response = self.session.post(
            STUDIO_EDIT_ENDPOINT,
            params={"alt": "json", "key": self.config["api_key"]},
            headers=self._headers(STUDIO_ORIGIN, f"{STUDIO_ORIGIN}/video/{video_id}/edit"),
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
        extension = body.get("responseContext", {}).get(
            "webResponseContextExtensionData", {}
        )
        if extension.get("challenge"):
            raise RuntimeError(
                "Studio 拒絕儲存資訊卡並要求額外驗證；"
                "HTTP 200 不代表已儲存。請重新從已登入的 YouTube Studio "
                "複製最新 Cookie 後再試。"
            )

        for attempt in range(4):
            if self._player_has_playlist_card(video_id, playlist_id):
                return
            if attempt < 3:
                time.sleep(2)
        raise RuntimeError(
            "Studio 回傳 HTTP 200，但回讀影片後仍找不到播放清單資訊卡；"
            "本次不記錄為完成。"
        )

    def _player_has_playlist_card(self, video_id: str, playlist_id: str) -> bool:
        response = self.session.get(
            "https://www.youtube.com/watch",
            params={"v": video_id},
            headers=self._headers(
                "https://www.youtube.com",
                f"https://www.youtube.com/watch?v={video_id}",
            ),
            timeout=30,
        )
        response.raise_for_status()
        player = self._json_assignment(response.text, "ytInitialPlayerResponse")
        cards = (player or {}).get("cards")
        return bool(cards and playlist_id in json.dumps(cards, ensure_ascii=False))


class StudioCardBrowser:
    """Use Studio's real editor so Google can issue its required attestation."""

    def __init__(self, raw_cookie: str, verifier: StudioPrivateClient) -> None:
        if not CHROME_BINARY.is_file():
            raise RuntimeError(f"找不到 Chrome：{CHROME_BINARY}")
        self.verifier = verifier
        options = ChromeOptions()
        options.binary_location = str(CHROME_BINARY)
        options.add_argument("--window-size=1600,1000")
        options.add_argument("--disable-notifications")
        options.add_argument("--headless=new")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/120.0.0.0"
        )
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 20)
        self.driver.get("https://www.youtube.com/")
        time.sleep(1)
        for name, value in StudioPrivateClient._parse_cookie(
            StudioPrivateClient._normalize_cookie(raw_cookie)
        ).items():
            if not name or name.startswith("*"):
                continue
            try:
                self.driver.add_cookie({
                    "name": name,
                    "value": value,
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": name.startswith("__Secure-") or name in ["SAPISID", "SSID", "HSID", "SID"],
                })
            except Exception:
                continue

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass

    def _js_click(self, element: Any) -> None:
        self.driver.execute_script("arguments[0].click();", element)

    def _execute_in_page_edit(self, video_id: str, edit_payload: dict[str, Any]) -> tuple[bool, str]:
        """Execute edit_video inside Studio Chrome session using authenticated fetch."""
        self.driver.get(f"https://studio.youtube.com/video/{video_id}/edit")
        try:
            self.wait.until(lambda d: d.execute_script(
                "return typeof window.ytcfg !== 'undefined' && Boolean(window.ytcfg.get('INNERTUBE_API_KEY'));"
            ))
        except Exception:
            time.sleep(3)

        script = """
        const callback = arguments[arguments.length - 1];
        const payload = arguments[0];
        try {
            const apiKey = (window.ytcfg && window.ytcfg.get('INNERTUBE_API_KEY')) || '';
            const clientVersion = (window.ytcfg && (window.ytcfg.get('INNERTUBE_CLIENT_VERSION') || window.ytcfg.get('INNERTUBE_CONTEXT_CLIENT_VERSION'))) || '1.20260829.00.00';
            const delegatedSessionId = (window.ytcfg && window.ytcfg.get('DELEGATED_SESSION_ID')) || '';
            const channelId = (window.ytcfg && window.ytcfg.get('CHANNEL_ID')) || '';

            if (!payload.context) {
                payload.context = {};
            }
            if (!payload.context.client) {
                payload.context.client = {
                    clientName: 62,
                    clientVersion: clientVersion,
                    hl: 'zh-TW',
                    gl: 'TW'
                };
            }
            if (!payload.context.request) {
                payload.context.request = { returnLogEntry: true, internalExperimentFlags: [] };
            }
            if (delegatedSessionId && !payload.context.user) {
                payload.context.user = { onBehalfOfUser: delegatedSessionId };
            }

            fetch('/youtubei/v1/video_editor/edit_video?alt=json&key=' + encodeURIComponent(apiKey), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-YouTube-Client-Name': '62',
                    'X-YouTube-Client-Version': clientVersion,
                },
                body: JSON.stringify(payload),
                credentials: 'include'
            })
            .then(async (r) => {
                const text = await r.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                callback({ status: r.status, ok: r.ok, text: text, json: json });
            })
            .catch((err) => {
                callback({ status: 0, ok: false, error: err.toString() });
            });
        } catch (e) {
            callback({ status: 0, ok: false, error: e.toString() });
        }
        """
        result = self.driver.execute_async_script(script, edit_payload)
        if isinstance(result, dict):
            if result.get("ok"):
                body = result.get("json") or {}
                ext = body.get("responseContext", {}).get("webResponseContextExtensionData", {})
                if ext.get("challenge"):
                    return False, "Studio 要求驗證 Challenge"
                if body.get("error"):
                    return False, f"API error: {body['error']}"
                return True, "OK"
            return False, result.get("error") or f"HTTP {result.get('status')}"
        return False, "無回傳結果"

    def set_navigation_cards(
        self,
        video_id: str,
        first_video_id: str,
        playlist_id: str,
        playlist_title: str,
        is_first_episode: bool = False,
    ) -> None:
        now_ms = int(time.time() * 1000)
        cards = []
        if not is_first_episode and first_video_id:
            # Card 1: Episode 1 link @ 0:03 (3000ms)
            cards.append({
                "videoId": video_id,
                "teaserStartMs": CARD_FIRST_EP_START_MS,
                "videoInfoCard": {
                    "fullVideoId": first_video_id,
                },
                "infoCardEntityId": str(now_ms),
                "customMessage": "第一次收聽？建議從第1集開始",
                "teaserText": "👉 點此從【第 1 集】開始聽",
            })
            # Card 2: Playlist link @ 0:14 (14000ms)
            cards.append({
                "videoId": video_id,
                "teaserStartMs": CARD_PLAYLIST_START_MS,
                "playlistInfoCard": {
                    "fullPlaylistId": playlist_id,
                },
                "infoCardEntityId": str(now_ms + 1),
                "customMessage": "完整播放清單",
                "teaserText": "📚 本部小說【完整播放清單】",
            })
        else:
            # Episode 1: Playlist link @ 0:03 (3000ms)
            cards.append({
                "videoId": video_id,
                "teaserStartMs": CARD_FIRST_EP_START_MS,
                "playlistInfoCard": {
                    "fullPlaylistId": playlist_id,
                },
                "infoCardEntityId": str(now_ms),
                "customMessage": "完整播放清單",
                "teaserText": "📚 本部小說【完整播放清單】",
            })

        channel_id = self.verifier.channel_id or CHANNEL_ID
        delegation_context = {
            "roleType": {"channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"},
            "externalChannelId": channel_id,
        } if channel_id else {}

        context = {
            "client": {
                "clientName": 62,
                "clientVersion": self.verifier.config.get("client_version") or "1.20260829.00.00",
                "hl": "zh-TW",
                "gl": "TW",
            },
            "request": {"returnLogEntry": True, "internalExperimentFlags": []},
            "user": {
                "delegationContext": delegation_context,
            },
        }

        payload = {
            "context": context,
            "externalVideoId": video_id,
            "infoCardEdit": {"infoCards": cards},
        }
        if delegation_context:
            payload["delegationContext"] = delegation_context

        ok, msg = self._execute_in_page_edit(video_id, payload)
        if ok:
            time.sleep(1)
            return

        # UI Fallback
        self._set_cards_via_ui(video_id, first_video_id, playlist_id, playlist_title, is_first_episode)

    def _set_cards_via_ui(
        self,
        video_id: str,
        first_video_id: str,
        playlist_id: str,
        playlist_title: str,
        is_first_episode: bool = False,
    ) -> None:
        self.driver.get(f"https://studio.youtube.com/video/{video_id}/edit")
        card_link = self.wait.until(EC.element_to_be_clickable((By.ID, "info-cards-editor-link")))
        self._js_click(card_link)
        time.sleep(1)
        try:
            delete_btns = self.driver.find_elements(
                By.XPATH,
                "//button[contains(@aria-label,'刪除') or contains(@aria-label,'Delete') or contains(@class,'delete')]"
            )
            for btn in delete_btns:
                if btn.is_displayed() and btn.is_enabled():
                    self._js_click(btn)
                    time.sleep(0.5)
        except Exception:
            pass

        choice = self.wait.until(EC.presence_of_element_located((
            By.XPATH,
            "//span[contains(@class,'info-card-type-option-label') and normalize-space(.)='播放清單']",
        )))
        self._js_click(choice)
        playlist_matches = self.wait.until(lambda driver: [
            element for element in driver.find_elements(
                By.XPATH,
                f"//*[normalize-space(.)={json.dumps(playlist_title, ensure_ascii=False)}]",
            )
            if element.is_displayed()
        ])
        self._js_click(playlist_matches[-1])
        time.sleep(1)
        save_buttons = self.wait.until(lambda driver: [
            element for element in driver.find_elements(By.XPATH, "//*[normalize-space(.)='儲存']")
            if element.is_displayed() and element.is_enabled()
        ])
        self._js_click(save_buttons[-1])
        time.sleep(2)

    def set_playlist_endscreen(
        self, video_id: str, playlist_id: str, playlist_title: str
    ) -> None:
        channel_id = self.verifier.channel_id or CHANNEL_ID
        delegation_context = {
            "roleType": {"channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"},
            "externalChannelId": channel_id,
        } if channel_id else {}

        context = {
            "client": {
                "clientName": 62,
                "clientVersion": self.verifier.config.get("client_version") or "1.20260829.00.00",
                "hl": "zh-TW",
                "gl": "TW",
            },
            "request": {"returnLogEntry": True, "internalExperimentFlags": []},
            "user": {
                "delegationContext": delegation_context,
            },
        }

        payload = {
            "context": context,
            "externalVideoId": video_id,
            "endscreenEdit": {
                "endscreen": {
                    "elements": [
                        {
                            "type": "PLAYLIST",
                            "playlistId": playlist_id,
                            "left": 0.58,
                            "top": 0.15,
                            "width": 0.40,
                            "aspectRatio": 1.7777777777777777,
                        }
                    ]
                }
            }
        }
        if delegation_context:
            payload["delegationContext"] = delegation_context

        ok, msg = self._execute_in_page_edit(video_id, payload)
        if ok:
            time.sleep(1)
            return

        # UI Fallback for Endscreen
        self._set_endscreen_via_ui(video_id, playlist_id, playlist_title)

    def _set_endscreen_via_ui(
        self, video_id: str, playlist_id: str, playlist_title: str
    ) -> None:
        self.driver.get(f"https://studio.youtube.com/video/{video_id}/edit")
        endscreen_link = self.wait.until(EC.element_to_be_clickable((
            By.XPATH,
            "//*[@id='endscreen-editor-link' or contains(text(),'片尾畫面') or contains(text(),'結束畫面')]"
        )))
        self._js_click(endscreen_link)
        time.sleep(1)
        choice = self.wait.until(EC.presence_of_element_located((
            By.XPATH,
            "//*[contains(text(),'播放清單') or contains(@class,'playlist')]",
        )))
        self._js_click(choice)
        playlist_matches = self.wait.until(lambda driver: [
            element for element in driver.find_elements(
                By.XPATH,
                f"//*[normalize-space(.)={json.dumps(playlist_title, ensure_ascii=False)}]",
            )
            if element.is_displayed()
        ])
        self._js_click(playlist_matches[-1])
        time.sleep(1)
        save_buttons = self.wait.until(lambda driver: [
            element for element in driver.find_elements(By.XPATH, "//*[normalize-space(.)='儲存']")
            if element.is_displayed() and element.is_enabled()
        ])
        self._js_click(save_buttons[-1])
        time.sleep(2)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        load_dotenv()
        self.title("YouTube 小說資訊卡／置頂留言補登工具")
        self.geometry("1120x760")
        self.minsize(920, 640)
        self.studio_client: StudioPrivateClient | None = None
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
        ttk.Label(top, text="目標頻道：").pack(side="left")
        self.channel_var = tk.StringVar(value=os.getenv("YOUTUBE_INFO_CARD_CHANNEL_ID", CHANNEL_ID))
        self.channel_entry = ttk.Entry(top, textvariable=self.channel_var, width=28)
        self.channel_entry.pack(side="left", padx=(0, 8))
        ttk.Button(top, text="讀取播放清單", command=self.load_playlists).pack(side="left", padx=4)

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
        self.do_endscreen = tk.BooleanVar(value=True)
        self.do_comment = tk.BooleanVar(value=True)
        self.do_pin = tk.BooleanVar(value=True)
        self.skip_done = tk.BooleanVar(value=True)
        self.include_first = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="資訊卡(0:03第1集+0:14清單)", variable=self.do_card).pack(side="left")
        ttk.Checkbutton(options, text="片尾清單", variable=self.do_endscreen).pack(side="left", padx=6)
        ttk.Checkbutton(options, text="導流留言", variable=self.do_comment).pack(side="left", padx=6)
        ttk.Checkbutton(options, text="置頂留言", variable=self.do_pin).pack(
            side="left", padx=6
        )
        ttk.Checkbutton(options, text="跳過已完成", variable=self.skip_done).pack(side="left", padx=6)
        ttk.Checkbutton(options, text="包含第一集", variable=self.include_first).pack(side="left", padx=6)

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="開始批次補登", command=self.start_batch)
        self.start_button.pack(side="left")
        ttk.Button(actions, text="停止", command=self.stop_batch).pack(side="left", padx=8)

        self.video_tree = ttk.Treeview(
            right, columns=("pos", "video", "card", "endscreen", "comment", "pin"), show="headings", height=18
        )
        for col, title, width in (
            ("pos", "集", 45),
            ("video", "影片", 330),
            ("card", "資訊卡", 80),
            ("endscreen", "片尾", 65),
            ("comment", "留言", 65),
            ("pin", "置頂", 65),
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
        raw_cookie = self.cookie_text.get("1.0", "end").strip()
        channel_id = self.channel_var.get().strip() or CHANNEL_ID
        if not raw_cookie:
            messagebox.showwarning("缺少 Cookie", "讀取播放清單也需要 YouTube Studio Cookie。")
            return
        def work() -> None:
            try:
                self.event_queue.put(("log", "正在透過 YouTube Studio 讀取播放清單…"))
                client = StudioPrivateClient(raw_cookie, channel_id=channel_id)
                if client.channel_id:
                    self.event_queue.put(("channel_id", client.channel_id))
                playlists = client.list_playlists()
                self.event_queue.put(("playlists", (client, playlists)))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
        self._run_thread(work)

    def _playlist_selected(self, _event=None) -> None:
        selection = self.playlist_tree.selection()
        if not selection or not self.studio_client:
            return
        index = int(selection[0])
        row = self.playlists[index]

        def work() -> None:
            try:
                videos = self.studio_client.list_playlist_videos(row.playlist_id)
                self.event_queue.put(("videos", (row, videos)))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
        self._run_thread(work)

    def start_batch(self) -> None:
        if not self.studio_client or not self.videos:
            messagebox.showwarning("尚未選擇", "請先讀取並選擇播放清單。")
            return
        raw_cookie = self.cookie_text.get("1.0", "end").strip()
        channel_id = self.channel_var.get().strip() or CHANNEL_ID
        if not raw_cookie:
            messagebox.showwarning("缺少 Cookie", "所有讀寫動作都需要 YouTube Studio Cookie。")
        self.start_button.configure(state="disabled")
        playlist = self.playlists[int(self.playlist_tree.selection()[0])]
        videos = list(self.videos)
        first = videos[0]
        do_card = self.do_card.get()
        do_endscreen = self.do_endscreen.get()
        do_comment = self.do_comment.get()
        do_pin = self.do_pin.get()
        skip_done = self.skip_done.get()
        include_first = self.include_first.get()

        def work() -> None:
            card_browser: StudioCardBrowser | None = None
            try:
                studio = StudioPrivateClient(raw_cookie)
                self.studio_client = studio
                if do_card or do_endscreen:
                    self.event_queue.put(("log", "正在啟動 Chrome 資訊卡／片尾編輯器…"))
                    card_browser = StudioCardBrowser(raw_cookie, studio)
                for video in videos:
                    if self.stop_event.is_set():
                        break
                    if video.position == 0 and not include_first:
                        continue
                    record = self.state_store.video(video.video_id)
                    self.event_queue.put(("log", f"處理 {video.position + 1}: {video.title}"))

                    is_first = (video.position == 0 or video.video_id == first.video_id)

                    if do_card:
                        if (
                            skip_done
                            and record.get("card_playlist_id") == playlist.playlist_id
                            and record.get("card_state_version") == CARD_STATE_VERSION
                        ):
                            self.event_queue.put(("status", (video.video_id, "card", "已完成")))
                        else:
                            card_browser.set_navigation_cards(
                                video.video_id,
                                first.video_id,
                                playlist.playlist_id,
                                playlist.title,
                                is_first_episode=is_first,
                            )
                            self.state_store.mark(video.video_id, "card_first_video_id", first.video_id)
                            self.state_store.mark(video.video_id, "card_playlist_id", playlist.playlist_id)
                            self.state_store.mark(video.video_id, "card_first_start_ms", CARD_FIRST_EP_START_MS)
                            self.state_store.mark(video.video_id, "card_playlist_start_ms", CARD_PLAYLIST_START_MS)
                            self.state_store.mark(
                                video.video_id, "card_state_version", CARD_STATE_VERSION
                            )
                            self.event_queue.put(("status", (video.video_id, "card", "OK")))

                    if do_endscreen:
                        if (
                            skip_done
                            and record.get("endscreen_playlist_id") == playlist.playlist_id
                            and record.get("endscreen_state_version") == ENDSCREEN_STATE_VERSION
                        ):
                            self.event_queue.put(("status", (video.video_id, "endscreen", "已完成")))
                        else:
                            card_browser.set_playlist_endscreen(
                                video.video_id, playlist.playlist_id, playlist.title
                            )
                            self.state_store.mark(video.video_id, "endscreen_playlist_id", playlist.playlist_id)
                            self.state_store.mark(
                                video.video_id, "endscreen_state_version", ENDSCREEN_STATE_VERSION
                            )
                            self.event_queue.put(("status", (video.video_id, "endscreen", "OK")))

                    comment_id = record.get("comment_id", "")
                    if do_comment or do_pin:
                        create_response = None
                        if not comment_id:
                            if do_comment:
                                comment_id, create_response = studio.post_navigation_comment(
                                    video.video_id, first.video_id, playlist.playlist_id
                                )
                                self.state_store.mark(video.video_id, "comment_id", comment_id)
                                self.event_queue.put(("status", (video.video_id, "comment", "OK")))
                            else:
                                self.event_queue.put(("log", f"⚠️ 跳過置頂：{video.title} 尚未建立導流留言"))
                                self.event_queue.put(("status", (video.video_id, "pin", "無留言")))
                        else:
                            self.event_queue.put(("status", (video.video_id, "comment", "已完成")))

                        if do_pin:
                            if skip_done and record.get("pinned_comment_id") == comment_id:
                                self.event_queue.put(("status", (video.video_id, "pin", "已完成")))
                            else:
                                studio.pin_comment(video.video_id, comment_id, create_response)
                                self.state_store.mark(video.video_id, "pinned_comment_id", comment_id)
                                self.event_queue.put(("status", (video.video_id, "pin", "OK")))

                self.event_queue.put(("done", "批次處理完成。" if not self.stop_event.is_set() else "已停止。"))
            except Exception as exc:
                self.event_queue.put(("error", str(exc)))
                self.event_queue.put(("done", "批次處理中止。"))
            finally:
                if card_browser:
                    card_browser.close()

        self._run_thread(work)

    def stop_batch(self) -> None:
        self.stop_event.set()
        self.log("收到停止指令，會在目前這支影片完成後停止。")

    def _update_status(self, video_id: str, field: str, value: str) -> None:
        for iid in self.video_tree.get_children():
            values = list(self.video_tree.item(iid, "values"))
            if len(values) < 6:
                continue
            hidden_id = self.video_tree.item(iid, "text")
            if hidden_id != video_id:
                continue
            index = {"card": 2, "endscreen": 3, "comment": 4, "pin": 5}[field]
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
                elif kind == "channel_id":
                    self.channel_var.set(str(payload))
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
                    self.log(f"已載入 {len(self.playlists)} 個播放清單。")
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
                        card_done = (
                            record.get("card_playlist_id") == row.playlist_id
                            and record.get("card_state_version") == CARD_STATE_VERSION
                        )
                        endscreen_done = (
                            record.get("endscreen_playlist_id") == row.playlist_id
                            and record.get("endscreen_state_version") == ENDSCREEN_STATE_VERSION
                        )
                        comment_done = bool(record.get("comment_id"))
                        pin_done = bool(record.get("pinned_comment_id"))
                        self.video_tree.insert(
                            "", "end", iid=str(idx), text=video.video_id,
                            values=(
                                video.position + 1,
                                video.title,
                                "已完成" if card_done else "",
                                "已完成" if endscreen_done else "",
                                "已完成" if comment_done else "",
                                "已完成" if pin_done else "",
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

def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
