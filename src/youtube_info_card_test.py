"""Isolated, auditable YouTube info-card experiment.

This is deliberately not imported by the production uploader.  It first probes
the discontinued/undocumented Data API ``infocards`` collection with the OAuth
credentials already used by this repository.  If that route is unavailable it
can fall back to YouTube Studio's own ``video_editor/edit_video`` endpoint when
a Studio browser session is supplied separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
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
    """Return enough of a response to diagnose it without leaking credentials."""
    text = response.text[:6000]
    text = re.sub(r'(?i)(access_token|refresh_token|sapisid|sid|authorization)["\s:=]+[^"\s,}]+', r"\1=<redacted>", text)
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
        except Exception as exc:  # the exception contains no secret values
            emit("oauth_refresh_failed", slot=slot, error=type(exc).__name__, detail=str(exc)[:500])
    return result


def api_get(creds: Credentials, resource: str, **params: str) -> requests.Response:
    return requests.get(
        f"{DATA_API}/{resource}", params=params,
        headers={"Authorization": f"Bearer {creds.token}"}, timeout=30,
    )


def owner_for_video(slots: list[tuple[int, Credentials]]) -> tuple[int, Credentials, dict[str, Any]]:
    for slot, creds in slots:
        channel_response = api_get(creds, "channels", part="id", mine="true")
        if not channel_response.ok:
            emit("channel_lookup_failed", slot=slot, response=safe_response(channel_response))
            continue
        channel_ids = {item["id"] for item in channel_response.json().get("items", [])}
        video_response = api_get(creds, "videos", part="snippet", id=VIDEO_ID)
        if not video_response.ok:
            emit("video_lookup_failed", slot=slot, response=safe_response(video_response))
            continue
        items = video_response.json().get("items", [])
        if items and items[0].get("snippet", {}).get("channelId") in channel_ids:
            emit("video_owner_found", slot=slot, channel_id=items[0]["snippet"]["channelId"], title=items[0]["snippet"].get("title"))
            return slot, creds, items[0]
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
                    creds, "playlistItems", part="snippet", playlistId=playlist["id"],
                    maxResults="50", pageToken=item_page,
                )
                items_response.raise_for_status()
                item_payload = items_response.json()
                if any(item.get("snippet", {}).get("resourceId", {}).get("videoId") == VIDEO_ID for item in item_payload.get("items", [])):
                    emit("playlist_selected", source="owned_playlist_scan", playlist_id=playlist["id"], title=playlist.get("snippet", {}).get("title"))
                    return playlist["id"]
                item_page = item_payload.get("nextPageToken", "")
                if not item_page:
                    break
        page = payload.get("nextPageToken", "")
        if not page:
            break
    raise RuntimeError("could not find an owned playlist containing the target video")


def probe_data_api(creds: Credentials, playlist_id: str) -> bool:
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
    params = {"part": "snippet", "videoId": VIDEO_ID}
    get_response = requests.get(f"{DATA_API}/infocards", params=params, headers=headers, timeout=30)
    emit("data_api_infocards_get", response=safe_response(get_response))
    if not get_response.ok:
        return False

    # Only mutate after the collection proves that it exists for this account.
    body = {
        "snippet": {
            "videoId": VIDEO_ID,
            "teaserStartMs": 10000,
            "playlistInfoCard": {"fullPlaylistId": playlist_id},
            "customMessage": "第一次看這部小說？",
            "teaserText": "從第一集開始觀看",
        }
    }
    post_response = requests.post(
        f"{DATA_API}/infocards", params={"part": "snippet"}, headers=headers,
        json=body, timeout=30,
    )
    emit("data_api_infocards_post", response=safe_response(post_response))
    return post_response.ok


def studio_cookie_map(raw: str) -> dict[str, str]:
    return {key.strip(): value.strip() for key, value in (part.split("=", 1) for part in raw.split(";") if "=" in part)}


def call_studio(playlist_id: str) -> bool:
    raw_cookies = os.getenv("YOUTUBE_STUDIO_COOKIES", "").strip()
    if not raw_cookies:
        emit("studio_unavailable", reason="missing GitHub Actions secret YOUTUBE_STUDIO_COOKIES")
        return False
    cookies = studio_cookie_map(raw_cookies)
    sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
    if not sapisid:
        emit("studio_unavailable", reason="YOUTUBE_STUDIO_COOKIES has no SAPISID or __Secure-3PAPISID")
        return False

    timestamp = str(int(time.time()))
    digest = hashlib.sha1(f"{timestamp} {sapisid} {STUDIO_ORIGIN}".encode()).hexdigest()
    context = {
        "client": {
            "clientName": "WEB_CREATOR",
            "clientVersion": os.getenv("YOUTUBE_STUDIO_CLIENT_VERSION", "1.20260826.00.00"),
            "hl": "zh-TW",
        }
    }
    payload = {
        "context": context,
        "externalVideoId": VIDEO_ID,
        "infoCardEdit": {"infoCards": [{
            "videoId": VIDEO_ID,
            "teaserStartMs": 10000,
            "playlistInfoCard": {"fullPlaylistId": playlist_id},
            "infoCardEntityId": str(int(time.time() * 1000)),
            "customMessage": "第一次看這部小說？",
            "teaserText": "從第一集開始觀看",
        }]},
    }
    response = requests.post(
        STUDIO_ENDPOINT,
        params={"key": os.getenv("YOUTUBE_STUDIO_API_KEY", "")},
        headers={
            "Authorization": f"SAPISIDHASH {timestamp}_{digest}",
            "Origin": STUDIO_ORIGIN,
            "Referer": f"{STUDIO_ORIGIN}/video/{VIDEO_ID}/edit",
            "X-Origin": STUDIO_ORIGIN,
            "X-Goog-AuthUser": os.getenv("YOUTUBE_STUDIO_AUTHUSER", "0"),
            "Content-Type": "application/json",
        },
        cookies=cookies,
        json=payload,
        timeout=30,
    )
    emit("studio_edit_video_post", response=safe_response(response))
    return response.ok


def main() -> int:
    emit("start", video_id=VIDEO_ID)
    slots = oauth_slots()
    emit("oauth_slots_refreshed", count=len(slots), slots=[slot for slot, _ in slots])
    if not slots:
        raise RuntimeError("no usable YouTube OAuth credential slots")
    slot, creds, video = owner_for_video(slots)
    playlist_id = discover_playlist(creds, video)
    if probe_data_api(creds, playlist_id):
        emit("result", success=True, route="data_api", owner_slot=slot)
        return 0
    emit("fallback", route="youtube_studio_internal_api")
    if call_studio(playlist_id):
        emit("result", success=True, route="studio_internal_api", owner_slot=slot)
        return 0
    emit("result", success=False, reason="both info-card routes failed or were unavailable")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit("fatal", error=type(exc).__name__, detail=str(exc)[:1000])
        raise
