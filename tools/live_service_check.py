"""Read-only external credential checks and real original-content media smoke test.

No uploaded videos, novel text, credentials, or raw tokens are published by this job.
"""
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

def check_hf_archive():
    from huggingface_hub import HfApi
    token = os.getenv("HF_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HF_TOKEN is not configured")
    api = HfApi(token=token)
    repo = os.getenv("HF_ARCHIVE_REPO", "").strip()
    if not repo:
        owner = api.whoami().get("name", "")
        if not owner:
            raise RuntimeError("HF account is inaccessible")
        repo = owner + "/audiobook-archive"
    info = api.repo_info(repo, repo_type="dataset", timeout=30)
    if not info.sha:
        raise RuntimeError("HF archive revision is not available")
    print("HF: archive read access confirmed", flush=True)


def check_youtube():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from src.youtube_upload.state import configured_youtube_account_slots

    required = int(os.getenv("YOUTUBE_EXPECTED_ACCOUNT_COUNT", "1"))
    configured = configured_youtube_account_slots()
    if any(slot not in configured for slot in range(1, required + 1)):
        raise RuntimeError("Missing complete YouTube OAuth credentials for required slot(s)")
    for slot in sorted(configured):
        credential = Credentials(
            token=None,
            refresh_token=os.environ[f"YOUTUBE_REFRESH_TOKEN_{slot}"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ[f"YOUTUBE_CLIENT_ID_{slot}"],
            client_secret=os.environ[f"YOUTUBE_CLIENT_SECRET_{slot}"],
        )
        credential.refresh(Request())
        service = build("youtube", "v3", credentials=credential, cache_discovery=False)
        channels = service.channels().list(part="id", mine=True, maxResults=1).execute()
        if not channels.get("items"):
            raise RuntimeError("YouTube slot lacks channel read access")
        print(f"YouTube slot {slot}: token refresh and channel read passed", flush=True)


def check_original_media():
    from src.artifact_validation import validate_video, validate_wav
    from src.subtitle_gen import generate_chapter_srt
    from src.video_gen import generate_chapter_video
    import edge_tts

    original = "這是一段原創影音產製測試文字，用來驗證字幕和影片的完整輸出。"
    async def speak(path):
        await asyncio.wait_for(
            edge_tts.Communicate(original, "zh-CN-YunjianNeural", rate="+25%").save(str(path)),
            timeout=50,
        )

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for folder in ("Audio", "Subtitles", "Images", "Video", "RawText"):
            (root / folder).mkdir()
        mp3 = root / "voice.mp3"
        wav = root / "Audio/Acceptance_chapter_1.wav"
        asyncio.run(speak(mp3))
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-ar", "24000",
             "-ac", "1", str(wav)],
            timeout=45, check=True,
        )
        validate_wav(wav)
        generate_chapter_srt([str(wav)], [original], str(root / "Subtitles/Acceptance_chapter_1.srt"))
        (root / "RawText/Acceptance_chapter_1_raw.txt").write_text(
            "第一章 原創測試\\n" + original, encoding="utf-8"
        )
        Image.new("RGB", (1280, 720), "navy").save(
            root / "Images/Acceptance_chapter_1.jpg", "JPEG"
        )
        video, duration = generate_chapter_video(
            "Acceptance", str(wav), str(root), str(root / "Video"), []
        )
        validate_video(video, duration)
        print("Original-content media: Edge TTS, WAV, SRT, FFmpeg MP4 verified", flush=True)


def main():
    failures = []
    for name, check in (
        ("HF archive", check_hf_archive),
        ("YouTube OAuth", check_youtube),
        ("TTS and video", check_original_media),
    ):
        try:
            check()
        except Exception as exc:
            # Do not include untrusted external response messages, which can
            # sometimes echo HTTP headers or account data.
            failures.append(name)
            print(f"{name}: FAIL ({type(exc).__name__})", flush=True)
    if failures:
        raise SystemExit("External verification incomplete: " + ", ".join(failures))


if __name__ == "__main__":
    main()
