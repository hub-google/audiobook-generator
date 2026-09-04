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

    def post_navigation_comment(self, video_id: str, first_video_id: str, playlist_id: str) -> tuple[str, dict[str, Any]]:
        if first_video_id and first_video_id != video_id:
            text = (
                f"{COMMENT_MARKER}\n🎧 第一次收聽這部小說？建議從第一集開始：\n"
                f"▶ 第一集：https://youtu.be/{first_video_id}\n\n"
                f"📚 完整播放清單：\nhttps://www.youtube.com/playlist?list={playlist_id}"
            )
        else:
            text = (
                f"{COMMENT_MARKER}\n🎧 歡迎收聽本部小說！可收藏完整小說播放清單隨時回聽：\n"
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

    @staticmethod
    def _has_navigation_cards(player_data: dict[str, Any] | None, playlist_id: str = "") -> bool:
        """Accurately check whether the player response contains genuine creator info cards.
        Excludes YouTube's default system notices (like error corrections).
        """
        if not player_data:
            return False
        cards = player_data.get("cards")
        if not cards or not isinstance(cards, dict):
            return False

        card_items = cards.get("cardCollectionRenderer", {}).get("cards", [])
        if not card_items:
            return False

        for item in card_items:
            cr = item.get("cardRenderer", {})
            teaser = cr.get("teaser", {})
            action = (
                teaser.get("simpleCardTeaserRenderer", {})
                .get("onTapCommand", {})
                .get("changeEngagementPanelVisibilityAction", {})
            )
            # 排除 YouTube 系統內建的「查看修正內容」提示面板
            if action.get("targetId") == "engagement-panel-error-corrections":
                continue

            cr_dump = json.dumps(cr, ensure_ascii=False)
            if playlist_id:
                if playlist_id in cr_dump:
                    return True
            else:
                if "content" in cr or any(
                    k in cr_dump
                    for k in (
                        "videoInfoCardContentRenderer",
                        "playlistInfoCardContentRenderer",
                        "linkInfoCardContentRenderer",
                        "channelInfoCardContentRenderer",
                    )
                ):
                    return True

        return False

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
        return self._has_navigation_cards(player, playlist_id)

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

            # 1. 獨立偵測：資訊卡 (排除系統提示，精準比對播放清單卡片)
            if self._has_navigation_cards(player_data, playlist_id):
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
                    if COMMENT_MARKER in body_str:
                        result["has_comment"] = True
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


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        self.title("YouTube 小說資訊卡／置頂留言補登工具")
        self.geometry("1160x820")
        self.minsize(960, 680)
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
        first = videos[0]
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
                for video in videos:
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
                                if any(kw in str(card_fail) for kw in ("身分驗證", "驗證逾時", "challenge", "Cookie 已過期", "尚未登入")):
                                    self.event_queue.put(("log", "⏸️ 由於 Google 安全驗證尚未完成，批次程序已自動暫停，以避免後續影片連鎖失敗。請在開啟的 Chrome 視窗內確認登入或完成身分驗證後，再點擊開始。"))
                                    break

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
                                comment_id, _ = studio.post_navigation_comment(
                                    video.video_id, first.video_id, playlist.playlist_id
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
    app.mainloop()


if __name__ == "__main__":
    main()
