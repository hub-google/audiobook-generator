"""Isolated, auditable YouTube info-card experiment.

This file is not imported by the production uploader. It probes, in order:
1. the historical Data API infocards collection;
2. YouTube Studio edit_video using the repository's existing OAuth token;
3. YouTube Studio edit_video using a browser session from Actions secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

VIDEO_ID = os.getenv("YOUTUBE_INFO_CARD_VIDEO_ID", "6vQeNPBUXEQ")
DATA_API = "https://www.googleapis.com/youtube/v3"
STUDIO_ORIGIN = "https://studio.youtube.com"
STUDIO_ENDPOINT = f"{STUDIO_ORIGIN}/youtubei/v1/video_editor/edit_video"
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def emit(stage: str, **values: Any) -> None:
    print(json.dumps({"stage": stage, **values}, ensure_ascii=False, sort_keys=True))


def safe_response(response: requests.Response) -> dict[str, Any]:
    text = response.text[:6000]
    text = re.sub(
        r'(?i)(access_token|refresh_token|sapisid|sid|authorization)["\s:=]+[^"\s,}]+',
        r"\1=<redacted>",
        text,
    )
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body": text,
    }


def oauth_slots() -> list[tuple[int, Credentials]]:
    result = []
    for slot in range(1, 11):
        client_id = os.getenv(f"YOUTUBE_CLIENT_ID_{slot}", "").strip()
        client_secret = os.getenv(f"YOUTUBE_CLIENT_SECRET_{slot}", "").strip()
        refresh_token = os.getenv(f"YOUTUBE_REFRESH_TOKEN_{slot}", "").strip()
        if not all((client_id, client_secret, refresh_token)):
            continue
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
        )
        try:
            creds.refresh(Request())
            result.append((slot, creds))
        except Exception as exc:
            emit("oauth_refresh_failed", slot=slot, error=type(exc).__name__, detail=str(exc)[:500])
    return result


def api_get(creds: Credentials, resource: str, **params: str) -> requests.Response:
    return requests.get(
        f"{DATA_API}/{resource}",
        params=params,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )


def owner_for_video(slots: list[tuple[int, Credentials]]) -> tuple[int, Credentials, dict[str, Any]]:
    video = None
    owner_channel_id = None
    for _, creds in slots:
        response = api_get(creds, "videos", part="snippet", id=VIDEO_ID)
        if response.ok and response.json().get("items"):
            video = response.json()["items"][0]
            owner_channel_id = video.get("snippet", {}).get("channelId")
            break
    if not video or not owner_channel_id:
        raise RuntimeError(f"could not read target video {VIDEO_ID}")

    for slot, creds in slots:
        channel_response = api_get(creds, "channels", part="id", mine="true")
        if not channel_response.ok:
            emit("channel_lookup_failed", slot=slot, response=safe_response(channel_response))
            continue
        channel_ids = {item["id"] for item in channel_response.json().get("items", [])}
        if owner_channel_id in channel_ids:
            emit(
                "video_owner_found",
                slot=slot,
                channel_id=owner_channel_id,
                title=video.get("snippet", {}).get("title"),
            )
            return slot, creds, video
    raise RuntimeError(f"none of the configured OAuth accounts owns video {VIDEO_ID}")


def discover_playlist(creds: Credentials, video: dict[str, Any]) -> str:
    forced = os.getenv("YOUTUBE_INFO_CARD_PLAYLIST_ID", "").strip()
    if forced:
        emit("playlist_selected", source="environment", playlist_id=forced)
        return forced

    description = video.get("snippet", {}).get("description", "")
    match = re.search(r"(?:[?&]list=|youtube\.com/playlist\?list=)([A-Za-z0-9_-]+)", description)
    if match:
        emit("playlist_selected", source="video_description", playlist_id=match.group(1))
        return match.group(1)

    page = ""
    while True:
        response = api_get(creds, "playlists", part="snippet", mine="true", maxResults="50", pageToken=page)
        response.raise_for_status()
        payload = response.json()
        for playlist in payload.get("items", []):
            item_page = ""
            while True:
                items_response = api_get(
                    creds,
                    "playlistItems",
                    part="snippet",
                    playlistId=playlist["id"],
                    maxResults="50",
                    pageToken=item_page,
                )
                items_response.raise_for_status()
                item_payload = items_response.json()
                if any(
                    item.get("snippet", {}).get("resourceId", {}).get("videoId") == VIDEO_ID
                    for item in item_payload.get("items", [])
                ):
                    emit(
                        "playlist_selected",
                        source="owned_playlist_scan",
                        playlist_id=playlist["id"],
                        title=playlist.get("snippet", {}).get("title"),
                    )
                    return playlist["id"]
                item_page = item_payload.get("nextPageToken", "")
                if not item_page:
                    break
        page = payload.get("nextPageToken", "")
        if not page:
            break
    raise RuntimeError("could not find an owned playlist containing the target video")


def probe_data_api_collection(creds: Credentials) -> requests.Response:
    response = requests.get(
        f"{DATA_API}/infocards",
        params={"part": "snippet", "videoId": VIDEO_ID},
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=30,
    )
    emit("data_api_infocards_get", response=safe_response(response))
    return response


def card_payload(playlist_id: str, client_version: str) -> dict[str, Any]:
    return {
        "context": {
            "client": {
                "clientName": "WEB_CREATOR",
                "clientVersion": client_version,
                "hl": "zh-TW",
            }
        },
        "externalVideoId": VIDEO_ID,
        "infoCardEdit": {
            "infoCards": [
                {
                    "videoId": VIDEO_ID,
                    "teaserStartMs": 10000,
                    "playlistInfoCard": {"fullPlaylistId": playlist_id},
                    "infoCardEntityId": str(int(time.time() * 1000)),
                    "customMessage": "第一次看這部小說？",
                    "teaserText": "從第一集開始觀看",
                }
            ]
        },
    }


def studio_bootstrap(cookies: dict[str, str] | None = None) -> tuple[str, str]:
    """Extract Studio's current API key/client version, using login cookies when available."""
    try:
        response = requests.get(
            f"{STUDIO_ORIGIN}/video/{VIDEO_ID}/edit",
            cookies=cookies or {},
            timeout=30,
        )
        text = response.text
        key_match = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', text)
        version_match = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', text)
        api_key = key_match.group(1) if key_match else ""
        version = version_match.group(1) if version_match else ""
        emit(
            "studio_bootstrap",
            authenticated=bool(cookies),
            status=response.status_code,
            api_key_found=bool(api_key),
            client_version=version,
        )
        return api_key, version
    except Exception as exc:
        emit("studio_bootstrap_failed", error=type(exc).__name__, detail=str(exc)[:500])
        return "", ""


def call_studio_with_oauth(creds: Credentials, playlist_id: str) -> bool:
    api_key, version = studio_bootstrap()
    client_version = version or "1.20260826.00.00"
    response = requests.post(
        STUDIO_ENDPOINT,
        params={"key": api_key} if api_key else {},
        headers={
            "Authorization": f"Bearer {creds.token}",
            "Origin": STUDIO_ORIGIN,
            "Referer": f"{STUDIO_ORIGIN}/video/{VIDEO_ID}/edit",
            "X-Origin": STUDIO_ORIGIN,
            "Content-Type": "application/json",
        },
        json=card_payload(playlist_id, client_version),
        timeout=30,
    )
    emit("studio_oauth_edit_video_post", response=safe_response(response))
    return response.ok


def studio_cookie_map(raw: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for key, value in (part.split("=", 1) for part in raw.split(";") if "=" in part)
    }


def call_studio_with_session(playlist_id: str) -> bool:
    raw_cookies = os.getenv("YOUTUBE_STUDIO_COOKIES", "").strip()
    if not raw_cookies:
        emit("studio_session_unavailable", reason="missing GitHub Actions secret YOUTUBE_STUDIO_COOKIES")
        return False

    cookies = studio_cookie_map(raw_cookies)
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    if not sapisid:
        emit("studio_session_unavailable", reason="Studio cookies contain no SAPISID or __Secure-3PAPISID")
        return False

    configured_key = os.getenv("YOUTUBE_STUDIO_API_KEY", "").strip()
    configured_version = os.getenv("YOUTUBE_STUDIO_CLIENT_VERSION", "").strip()
    detected_key, detected_version = studio_bootstrap(cookies)
    api_key = configured_key or detected_key
    client_version = configured_version or detected_version or "1.20260826.00.00"

    timestamp = str(int(time.time()))
    digest = hashlib.sha1(f"{timestamp} {sapisid} {STUDIO_ORIGIN}".encode()).hexdigest()
    response = requests.post(
        STUDIO_ENDPOINT,
        params={"key": api_key} if api_key else {},
        headers={
            "Authorization": f"SAPISIDHASH {timestamp}_{digest}",
            "Origin": STUDIO_ORIGIN,
            "Referer": f"{STUDIO_ORIGIN}/video/{VIDEO_ID}/edit",
            "X-Origin": STUDIO_ORIGIN,
            "X-Goog-AuthUser": os.getenv("YOUTUBE_STUDIO_AUTHUSER", "0"),
            "Content-Type": "application/json",
        },
        cookies=cookies,
        json=card_payload(playlist_id, client_version),
        timeout=30,
    )
    emit("studio_session_edit_video_post", response=safe_response(response))
    return response.ok


def main() -> int:
    emit("start", video_id=VIDEO_ID)
    slots = oauth_slots()
    emit("oauth_slots_refreshed", count=len(slots), slots=[slot for slot, _ in slots])
    if not slots:
        raise RuntimeError("no usable YouTube OAuth credential slots")

    probe_data_api_collection(slots[0][1])
    slot, creds, video = owner_for_video(slots)
    playlist_id = discover_playlist(creds, video)

    if call_studio_with_oauth(creds, playlist_id):
        emit("result", success=True, route="studio_internal_api_oauth", owner_slot=slot)
        return 0

    emit("fallback", route="youtube_studio_session")
    if call_studio_with_session(playlist_id):
        emit("result", success=True, route="studio_internal_api_session", owner_slot=slot)
        return 0

    emit(
        "result",
        success=False,
        reason="Studio OAuth was rejected and Studio browser-session secret is unavailable or failed",
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit("fatal", error=type(exc).__name__, detail=str(exc)[:1000])
        raise
