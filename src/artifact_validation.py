"""Strict validators and fingerprints for resumable audiobook artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import wave


SRT_TIMING = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


class ArtifactValidationError(RuntimeError):
    pass


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _timestamp_seconds(groups):
    hours, minutes, seconds, millis = (int(value) for value in groups)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def validate_text(path, clean=False):
    text = _read_text(path)
    meaningful = "".join(text.split())
    minimum = 20 if clean else 30
    if len(meaningful) < minimum:
        raise ArtifactValidationError(f"text is too short ({len(meaningful)} characters)")
    lowered = meaningful.lower()
    # A single word such as "驗證碼" can be legitimate novel dialogue.
    # Require corroborating anti-bot/error-page signals instead of rejecting
    # an entire chapter for one incidental word.
    blocked = ("captcha", "cloudflare", "accessdenied", "驗證碼", "存取遭拒")
    matched = {marker for marker in blocked if marker in lowered}
    if len(matched) >= 2:
        raise ArtifactValidationError("text resembles an anti-bot or error page")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not clean and len(lines) < 2:
        raise ArtifactValidationError("raw chapter has no title/body separation")
    if clean and not lines:
        raise ArtifactValidationError("clean chapter has no TTS segments")
    return {
        "bytes": os.path.getsize(path),
        "characters": len(meaningful),
        "lines": len(lines),
        "sha256": sha256_file(path),
    }


def validate_wav(path):
    try:
        with wave.open(path, "rb") as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
    except (wave.Error, EOFError, OSError) as error:
        raise ArtifactValidationError(f"WAV cannot be decoded: {error}") from error
    duration = frames / float(rate) if rate else 0.0
    if duration <= 0.25 or rate <= 0 or channels not in (1, 2) or sample_width not in (1, 2, 3, 4):
        raise ArtifactValidationError(
            f"invalid WAV properties: duration={duration}, rate={rate}, channels={channels}"
        )
    return {
        "bytes": os.path.getsize(path), "duration_seconds": round(duration, 3),
        "sample_rate": rate, "channels": channels, "sample_width": sample_width,
        "sha256": sha256_file(path),
    }


def validate_srt(path, audio_duration=None):
    text = _read_text(path).replace("\r\n", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    if not blocks:
        raise ArtifactValidationError("SRT has no cues")
    previous_end = -1.0
    last_end = 0.0
    for expected_index, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ArtifactValidationError(f"invalid SRT cue {expected_index}")
        match = SRT_TIMING.match(lines[1].strip())
        if not match:
            raise ArtifactValidationError(f"invalid SRT timing at cue {expected_index}")
        start = _timestamp_seconds(match.groups()[:4])
        end = _timestamp_seconds(match.groups()[4:])
        if start < previous_end - 0.01 or end <= start:
            raise ArtifactValidationError(f"non-monotonic SRT timing at cue {expected_index}")
        if not "".join(lines[2:]).strip():
            raise ArtifactValidationError(f"empty SRT text at cue {expected_index}")
        previous_end = end
        last_end = end
    if audio_duration is not None and last_end > float(audio_duration) + 1.0:
        raise ArtifactValidationError(
            f"SRT ends at {last_end:.3f}s after audio ends at {audio_duration:.3f}s"
        )
    return {
        "bytes": os.path.getsize(path), "cue_count": len(blocks),
        "end_seconds": round(last_end, 3), "sha256": sha256_file(path),
    }


def validate_image(path, expected_size=(1280, 720)):
    try:
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            size = tuple(image.size)
            image_format = image.format
    except Exception as error:
        raise ArtifactValidationError(f"image cannot be decoded: {error}") from error
    if expected_size and size != tuple(expected_size):
        raise ArtifactValidationError(f"image size is {size}, expected {expected_size}")
    return {
        "bytes": os.path.getsize(path), "width": size[0], "height": size[1],
        "format": image_format, "sha256": sha256_file(path),
    }


def _ffprobe(path):
    executable = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run(
        [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise ArtifactValidationError(f"ffprobe failed: {result.stderr.strip()[-500:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ArtifactValidationError("ffprobe returned invalid JSON") from error


def validate_video(path, audio_duration=None):
    probe = _ffprobe(path)
    streams = probe.get("streams") or []
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if not video_streams or not audio_streams:
        raise ArtifactValidationError("MP4 must contain both video and audio streams")
    try:
        duration = float((probe.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        raise ArtifactValidationError("MP4 duration is unavailable")
    if duration <= 0.25:
        raise ArtifactValidationError("MP4 duration is invalid")
    if audio_duration is not None and abs(duration - float(audio_duration)) > max(2.0, float(audio_duration) * 0.01):
        raise ArtifactValidationError(
            f"MP4/WAV duration mismatch: video={duration:.3f}s audio={audio_duration:.3f}s"
        )
    return {
        "bytes": os.path.getsize(path), "duration_seconds": round(duration, 3),
        "video_codec": video_streams[0].get("codec_name"),
        "audio_codec": audio_streams[0].get("codec_name"),
        "sha256": sha256_file(path),
    }


def validate_stage(stage, path, workspace_dir=None, chapter=None, book_title=None):
    if not os.path.exists(path):
        raise ArtifactValidationError(f"required output is missing: {path}")
    if stage == "crawler":
        return validate_text(path, clean=False)
    if stage == "cleaner":
        return validate_text(path, clean=True)
    if stage == "tts":
        return validate_wav(path)
    if stage == "subtitle":
        wav_path = os.path.join(workspace_dir, "Audio", f"{book_title}_chapter_{chapter}.wav")
        duration = validate_wav(wav_path)["duration_seconds"] if os.path.exists(wav_path) else None
        return validate_srt(path, duration)
    if stage == "image":
        return validate_image(path)
    if stage == "video":
        wav_path = os.path.join(workspace_dir, "Audio", f"{book_title}_chapter_{chapter}.wav")
        duration = validate_wav(wav_path)["duration_seconds"] if os.path.exists(wav_path) else None
        return validate_video(path, duration)
    raise ValueError(f"unknown stage: {stage}")
