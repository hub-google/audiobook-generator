"""Read-only external checks plus a small original-text production media smoke test."""
import asyncio
import os
import tempfile
import subprocess
from pathlib import Path

from huggingface_hub import HfApi
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from PIL import Image
import edge_tts

from src.artifact_validation import validate_video, validate_wav
from src.subtitle_gen import generate_chapter_srt
from src.video_gen import generate_chapter_video


def check_hf():
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HF token unavailable")
    api = HfApi(token=token)
    repo = os.environ.get("HF_ARCHIVE_REPO", "").strip() or api.whoami()["name"] + "/audiobook-archive"
    info = api.repo_info(repo, repo_type="dataset", timeout=30)
    if not info.sha:
        raise RuntimeError("HF archive revision unavailable")
    print("HF archive: read access and revision verified", flush=True)


def check_youtube():
    configured = 0
    refreshed = 0
    read_access = 0
    for slot in range(1, 11):
        values = [os.environ.get(f"YOUTUBE_{key}_{slot}", "").strip() for key in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")]
        if not all(values):
            continue
        configured += 1
        try:
            cred = Credentials(None, refresh_token=values[2], token_uri="https://oauth2.googleapis.com/token", client_id=values[0], client_secret=values[1])
            cred.refresh(Request())
            refreshed += 1
            service = build("youtube", "v3", credentials=cred, cache_discovery=False)
            service.channels().list(part="id", mine=True, maxResults=1).execute()
            read_access += 1
        except Exception as exc:
            # Never print token endpoint payloads or credential values.
            category = next((code for code in ("invalid_grant", "invalid_client", "invalid_scope", "unauthorized_client") if code in str(exc)), type(exc).__name__)
            print(f"YouTube slot {slot}: {category}", flush=True)
    print(f"YouTube credentials: {refreshed}/{configured} refreshed; {read_access}/{configured} read API passed", flush=True)
    if not configured or refreshed != configured or read_access != configured:
        raise RuntimeError("YouTube read-only check incomplete")


async def speak(text, target):
    await asyncio.wait_for(edge_tts.Communicate(text, "zh-CN-YunjianNeural", rate="+25%").save(str(target)), timeout=35)


def check_media():
    text = "這是一段原創測試文字，今天我們驗證語音字幕和影片。"
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for name in ("Audio", "Subtitles", "Images", "Video", "RawText"):
            (root/name).mkdir()
        book = "Acceptance"
        mp3 = root/"speech.mp3"
        wav = root/"Audio/Acceptance_chapter_1.wav"
        asyncio.run(speak(text, mp3))
        subprocess.run(["ffmpeg","-v","error","-y","-i",str(mp3),"-ar","24000","-ac","1",str(wav)],check=True,timeout=30)
        validate_wav(wav)
        generate_chapter_srt([str(wav)], [text], str(root/"Subtitles/Acceptance_chapter_1.srt"))
        (root/"RawText/Acceptance_chapter_1_raw.txt").write_text("第1章 測試\n"+text,encoding="utf-8")
        Image.new("RGB",(1280,720),"navy").save(root/"Images/Acceptance_chapter_1.jpg","JPEG")
        video, duration = generate_chapter_video(book, str(wav), str(root), str(root/"Video"), [])
        validate_video(video, duration)
        print("Production container: actual Edge TTS, SRT and FFmpeg MP4 verified", flush=True)


def main():
    failed = []
    for name, check in [("HF",check_hf),("YouTube",check_youtube),("TTS and video",check_media)]:
        try:
            check()
        except Exception as exc:
            failed.append(name)
            print(f"{name}: FAIL ({type(exc).__name__})", flush=True)
    if failed:
        raise SystemExit("Incomplete external checks: " + ", ".join(failed))


if __name__ == "__main__":
    main()
