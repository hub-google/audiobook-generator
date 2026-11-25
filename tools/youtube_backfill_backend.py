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


class StudioPrivateClient:
    """Minimal local implementation of the Studio edit_video and comment requests."""

    def __init__(self, raw_cookie: str) -> None:
        self.raw_cookie = self._normalize_cookie(raw_cookie)
        self.cookies = self._parse_cookie(self.raw_cookie)
        self.session = requests.Session()
        self.session.cookies.update(self.cookies)
        self.config: dict[str, str] = {}
        self._bootstrap()

    @staticmethod
    def _normalize_cookie(raw: str) -> str:
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1].strip()
        value = re.sub(r"^\s*cookie\s*:\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\\([_*])", r"\1", value)
        value = re.sub(r"[\r\n]+", " ", value)
        return value.strip()

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
        if not (
            self.cookies.get("SAPISID")
            or self.cookies.get("__Secure-3PAPISID")
            or self.cookies.get("__Secure-1PAPISID")
        ):
            raise RuntimeError("Cookie 缺少 SAPISID / __Secure-3PAPISID / __Secure-1PAPISID。")

        studio_url = f"{STUDIO_ORIGIN}/channel/{CHANNEL_ID}"
        response = self.session.get(
            studio_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
            timeout=30,
        )
        response.raise_for_status()
        if not response.url.startswith(STUDIO_ORIGIN) or "signin" in response.url or "accounts.google.com" in response.url:
            raise RuntimeError("YouTube Studio Cookie 已過期或失效（已被重新導向至登入頁面）。請點擊『瀏覽器自動擷取 Cookie』重新登入。")
        text = response.text
        studio_text = text
        fallback = self.session.get(
            "https://www.youtube.com/",
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
            timeout=30,
        )
        fallback.raise_for_status()
        fallback_text = fallback.text
        text = f"{studio_text}\n{fallback_text}"
        datasync = self._config_value(text, "DATASYNC_ID")
        sync_first, sep, sync_second = datasync.partition("||")
        self.config = {
            "api_key": self._config_value(studio_text, "INNERTUBE_API_KEY")
            or self._config_value(fallback_text, "INNERTUBE_API_KEY"),
            "client_version": self._config_value(text, "INNERTUBE_CLIENT_VERSION")
            or self._config_value(text, "INNERTUBE_CONTEXT_CLIENT_VERSION")
            or "1.20260826.03.00",
            "web_client_version": self._config_value(
                fallback_text, "INNERTUBE_CLIENT_VERSION"
            )
            or self._config_value(
                fallback_text, "INNERTUBE_CONTEXT_CLIENT_VERSION"
            )
            or "2.20260826.00.00",
            "auth_user": self._config_value(text, "SESSION_INDEX") or "0",
            "page_id": self._config_value(text, "DELEGATED_SESSION_ID")
            or (sync_first if sep and sync_second else ""),
            "identity_token": self._config_value(text, "ID_TOKEN"),
            "visitor_data": self._config_value(text, "VISITOR_DATA")
            or self._config_value(text, "visitorData"),
            "user_session_id": self._config_value(studio_text, "USER_SESSION_ID"),
        }
        if not self.config["api_key"]:
            raise RuntimeError(
                "無法取得 INNERTUBE_API_KEY；請確認網路可連線至 YouTube。"
            )

    def _authorization(self, origin: str = STUDIO_ORIGIN) -> str:
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
                f"{prefix}{timestamp} {sid} {origin}".encode("utf-8")
            ).hexdigest()
            values.append(f"{scheme} {timestamp}_{digest}{suffix}")
        return " ".join(values)

    def _headers(
        self, origin: str = STUDIO_ORIGIN, referer: str | None = None
    ) -> dict[str, str]:
        headers = {
            "Authorization": self._authorization(origin),
            "Origin": origin,
            "Referer": referer or f"{origin}/",
            "X-Origin": origin,
            "X-Goog-AuthUser": self.config.get("auth_user", "0"),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.config.get("page_id"):
            headers["X-Goog-PageId"] = self.config["page_id"]
        if self.config.get("identity_token"):
            headers["X-Youtube-Identity-Token"] = self.config["identity_token"]
        if self.config.get("visitor_data"):
            headers["X-Goog-Visitor-Id"] = self.config["visitor_data"]
        return headers

    def _context(self) -> dict[str, Any]:
        delegation_context = {
            "roleType": {"channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"},
            "externalChannelId": CHANNEL_ID,
        }
        return {
            "client": {
                "clientName": 62,
                "clientVersion": self.config["client_version"],
                "hl": "zh-TW",
                "gl": "TW",
            },
            "request": {"returnLogEntry": True, "internalExperimentFlags": []},
            "user": {
                **({"onBehalfOfUser": self.config["page_id"]} if self.config.get("page_id") else {}),
                "delegationContext": delegation_context,
                "serializedDelegationContext": "",
            },
        }

    def _web_context(self) -> dict[str, Any]:
        context = self._context()
        context["client"] = {
            "clientName": 1,
            "clientVersion": self.config["web_client_version"],
            "hl": "zh-TW",
            "gl": "TW",
        }
        return context

    def _studio_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{STUDIO_ORIGIN}/youtubei/v1/{path}",
            params={"alt": "json", "key": self.config["api_key"]},
            headers=self._headers(), json=payload, timeout=30,
        )
        if not response.ok:
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
            delegation = {
                "externalChannelId": CHANNEL_ID,
                "roleType": {
                    "channelRoleType": "CREATOR_CHANNEL_ROLE_TYPE_OWNER"
                },
            }
            body = self._studio_post("creator/list_creator_playlists", {
                "context": self._context(),
                "channelId": CHANNEL_ID,
                "delegationContext": delegation,
                "mask": {"playlistId": True, "title": True, "videoCount": True},
                "memberVideoIds": [],
                "pageSize": 500,
                "pageToken": page_token,
            })
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
            if isinstance(body.get("playlists"), list):
                return []
            keys = ", ".join(sorted(body.keys()))
            raise RuntimeError(
                f"Studio 回應沒有播放清單資料（回應欄位：{keys or '無'}）。"
            )
        return list(found.values())

    def list_playlist_videos(self, playlist_id: str) -> list[VideoRow]:
        page_token = ""
        raw_rows: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        while True:
            body = self._studio_post("creator/list_creator_videos", {
                "context": self._context(),
                "channelIds": [CHANNEL_ID],
                "pageSize": 500,
                "pageToken": page_token,
                "filter": {"and": {"operands": [
                    {"channelIdIs": {"value": CHANNEL_ID}},
                    {"playlistIdIs": {"value": playlist_id}},
                ]}},
                "order": "VIDEO_ORDER_DISPLAY_TIME_DESC",
                "mask": {
                    "videoId": True,
                    "title": True,
                    "titleFormattedString": {"all": True},
                    "timeCreatedSeconds": True,
                    "timePublishedSeconds": True,
                },
            })
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

        raw_rows.sort(key=lambda row: (row[0] == 0, row[0], row[2]))
        rows = [
            VideoRow(video_id, title, position)
            for position, (_, video_id, title) in enumerate(raw_rows)
        ]
        if not rows:
            if isinstance(body.get("videos"), list):
                return []
            keys = ", ".join(sorted(body.keys()))
            raise RuntimeError(
                f"Studio 回應沒有此播放清單的影片（回應欄位：{keys or '無'}）。"
            )
        return rows

    @classmethod
    def _create_comment_params(cls, video_id: str) -> str:
        raw = bytes((0x12, len(video_id))) + video_id.encode() + bytes((0x2A, 0, 0x50, 7))
        return base64.b64encode(raw).decode()

    @classmethod
    def _build_pin_action_token(cls, comment_id: str, video_id: str, channel_id: str = CHANNEL_ID) -> str:
        """Construct exact YouTube comment pin action protobuf token."""
        buf = bytearray()
        buf.extend(b"\x08\x0b\x10\x02")
        c_bytes = comment_id.encode("utf-8")
        buf.extend(bytes([0x1a, len(c_bytes)]) + c_bytes)
        v_bytes = video_id.encode("utf-8")
        buf.extend(bytes([0x2a, len(v_bytes)]) + v_bytes)
        buf.extend(b"\x30\x00\xa8\x01\x0c")
        ch_bytes = channel_id.encode("utf-8")
        buf.extend(b"\xba\x01" + bytes([len(ch_bytes)]) + ch_bytes)
        buf.extend(b"\xf0\x01\x00\x8a\x02\x10comments-section\xf8\x02\x01\xb0\x03\x00\xc8\x03\x00")
        return base64.urlsafe_b64encode(buf).decode("ascii").rstrip("=")

    def post_navigation_comment(
        self,
        video_id: str,
        first_video_id: str,
        playlist_id: str,
        comment_text: str = "",
    ) -> tuple[str, dict[str, Any]]:
        text = comment_text.strip()
        if not text:
            if first_video_id and first_video_id != video_id:
                text = (
                    f"🎧 【從第1部開始聽】：https://www.youtube.com/watch?v={first_video_id}\n"
                    f"📚  完整播放清單：https://www.youtube.com/playlist?list={playlist_id}"
                )
            else:
                text = f"📚  完整播放清單：https://www.youtube.com/playlist?list={playlist_id}"
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

    def pin_comment(
        self,
        video_id: str,
        comment_id: str,
        create_response: dict[str, Any] | None = None,
    ) -> None:
        """Pin the navigation comment using native protobuf action token with consistency retry."""
        token = self._build_pin_action_token(comment_id, video_id)
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                res = self._youtube_post("comment/perform_comment_action", {
                    "context": self._web_context(), "actions": [token],
                }, video_id)
                
                # Check actionResults for status
                results = res.get("actionResults", [])
                if results and isinstance(results, list):
                    first_res = results[0]
                    if isinstance(first_res, dict) and first_res.get("status") in ("STATUS_FAILED", "ERROR"):
                        raise RuntimeError(f"置頂失敗: {first_res.get('feedback', '未知錯誤')}")
                return
            except Exception as exc:
                last_err = exc
                err_str = str(exc)
                if "404" in err_str or "not found" in err_str.lower():
                    # YouTube backend needs ~1.2s eventual consistency for freshly posted comments
                    time.sleep(1.2)
                    continue
                raise exc
        if last_err:
            raise last_err

    def set_navigation_card(
        self, video_id: str, playlist_id: str, first_video_id: str = ""
    ) -> None:
        cards = []
        is_first_episode = bool(first_video_id and first_video_id == video_id)

        # Card 1: 0:03 (3,000ms) - 第一集導流資訊卡（僅在非第一集時掛載）
        if not is_first_episode and first_video_id:
            card1 = {
                "videoId": video_id,
                "teaserStartMs": CARD_1_START_MS,
                "videoInfoCard": {
                    "videoId": first_video_id,
                },
                "infoCardEntityId": str(int(time.time() * 1000)),
                "customMessage": "第一次收聽？從第一集開始",
                "teaserText": "第一次收聽？從第一集開始",
            }
            cards.append(card1)

        # Card 2: 0:13 (13,000ms) - 完整播放清單資訊卡
        card2 = {
            "videoId": video_id,
            "teaserStartMs": CARD_2_START_MS,
            "playlistInfoCard": {
                "fullPlaylistId": playlist_id,
            },
            "infoCardEntityId": str(int(time.time() * 1000) + 1),
            "customMessage": "完整小說播放清單",
            "teaserText": "完整小說播放清單",
        }
        cards.append(card2)

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
            "infoCardEdit": {"infoCards": cards},
        }

        request_time_ms = str(int(time.time() * 1000))
        ad_signals = (
            f"dt={request_time_ms}&flash=0&frm&u_tz=480&u_his=2&u_h=720&u_w=1280&"
            "u_ah=672&u_aw=1280&u_cd=24&bc=31&bih=551&biw=382&"
            "brdim=0%2C0%2C0%2C0%2C1280%2C0%2C1280%2C672%2C382%2C551&vis=1&wgl=true&ca_type=image"
        )
        headers = self._headers(
            STUDIO_ORIGIN,
            referer=f"{STUDIO_ORIGIN}/video/{video_id}/edit",
        )
        headers.update({
            "X-Youtube-Client-Name": "62",
            "X-Youtube-Client-Version": self.config.get("client_version") or "1.20260826.03.00",
            "X-Youtube-Bootstrap-Logged-In": "true",
            "X-Goog-Request-Time": request_time_ms,
            "X-Youtube-Ad-Signals": ad_signals,
            "X-Youtube-Page-CL": "971371204",
            "X-Youtube-Page-Label": "youtube.studio.web_20260826_03_RC00",
            "X-Youtube-Time-Zone": "Asia/Taipei",
            "X-Youtube-Utc-Offset": "480",
        })

        response = self.session.post(
            STUDIO_EDIT_ENDPOINT,
            params={"alt": "json", "key": self.config["api_key"]},
            headers=headers,
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

        # Confirm that the player response has the cards
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
        return bool(cards and (playlist_id in json.dumps(cards, ensure_ascii=False) or "infoCard" in json.dumps(cards, ensure_ascii=False)))

    def check_video_online_status(
        self, video_id: str, playlist_id: str
    ) -> dict[str, Any]:
        """Independently probe YouTube watch page for:
        1. has_card: Info card exists on video.
        2. has_comment: Owner's navigation comment exists.
        3. has_pin: That navigation comment is currently pinned.
        """
        result = {"has_card": False, "has_comment": False, "has_pin": False, "comment_id": ""}
        try:
            url = f"https://www.youtube.com/watch?v={video_id}"
            response = self.session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"},
                timeout=15,
            )
            if not response.ok:
                return result

            text = response.text
            player_data = self._json_assignment(text, "ytInitialPlayerResponse")
            initial_data = self._json_assignment(text, "ytInitialData")

            # 1. 獨立偵測：資訊卡
            cards = (player_data or {}).get("cards")
            if cards and (playlist_id in json.dumps(cards, ensure_ascii=False) or "infoCard" in json.dumps(cards, ensure_ascii=False)):
                result["has_card"] = True

            # 2. 獨立偵測：導流留言 & 置頂狀態 (精準解析 comments-section continuation token)
            token = ""
            if initial_data:
                for node in self._walk(initial_data):
                    if isinstance(node, dict) and node.get("sectionIdentifier") in ("comment-item-section", "comments-section"):
                        toks = self._continuation_tokens(node)
                        if toks:
                            token = toks[0]
                            break
                if not token:
                    toks = self._continuation_tokens(initial_data)
                    if toks:
                        token = toks[0]

            if token:
                try:
                    body = self._youtube_post(
                        "next",
                        {"context": self._web_context(), "continuation": token},
                        video_id,
                    )
                    body_str = json.dumps(body, ensure_ascii=False)
                    has_nav = (
                        COMMENT_MARKER in body_str
                        or "從第1部開始聽" in body_str
                        or ("完整播放清單" in body_str and playlist_id in body_str)
                    )
                    if has_nav:
                        result["has_comment"] = True
                        for node in self._walk(body):
                            if isinstance(node, dict) and "commentViewModel" in node:
                                n_str = json.dumps(node, ensure_ascii=False)
                                if (
                                    COMMENT_MARKER in n_str
                                    or "從第1部開始聽" in n_str
                                    or ("完整播放清單" in n_str and playlist_id in n_str)
                                ):
                                    cid = node["commentViewModel"].get("commentId")
                                    if cid:
                                        result["comment_id"] = cid
                                        break
                        if not result["comment_id"]:
                            for node in self._walk(body):
                                if isinstance(node, dict) and "commentViewModel" in node:
                                    cid = node["commentViewModel"].get("commentId")
                                    if cid:
                                        result["comment_id"] = cid
                                        break
                        if (
                            "RENDERING_PRIORITY_PINNED_COMMENT" in body_str
                            or "pinnedText" in body_str
                            or "pinnedCommentBadge" in body_str
                        ):
                            result["has_pin"] = True
                except Exception:
                    pass
        except Exception:
            pass
        return result


