"""Strict validators and fingerprints for resumable audiobook artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import wave
import threading
import unicodedata
from datetime import datetime, timezone


SRT_TIMING = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+"
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


class ArtifactValidationError(RuntimeError):
    pass


def stable_signature(value):
    """Return a deterministic SHA256 for JSON-compatible stage inputs/settings."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class ArtifactRegistry:
    """Durable cache of *completed strict validations*, keyed by immutable file identity.

    Size/mtime/file-id are only a cache discriminator.  They never constitute a
    successful validation by themselves; a cache miss always invokes the full validator.
    """

    def __init__(self, path, enabled=True):
        self.path = os.path.abspath(path)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        try:
            with open(self.path, encoding="utf-8") as handle:
                self.data = json.load(handle)
            if not isinstance(self.data.get("artifacts"), dict):
                raise ValueError("invalid artifact registry")
        except (OSError, ValueError, json.JSONDecodeError):
            self.data = {"schema_version": 1, "artifacts": {}}

    @staticmethod
    def identity(path):
        stat = os.stat(path)
        return {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "file_id": getattr(stat, "st_ino", 0),
        }

    def validate(self, path, validator, *, validator_key, input_signature="",
                 settings_signature="", **validator_kwargs):
        absolute = os.path.abspath(path)
        identity = self.identity(absolute)
        key = os.path.normcase(absolute)
        with self._lock:
            record = self.data["artifacts"].get(key) or {}
            if (self.enabled and record.get("identity") == identity
                    and record.get("validator_key") == validator_key
                    and record.get("input_signature", "") == input_signature
                    and record.get("settings_signature", "") == settings_signature
                    and record.get("validation", {}).get("sha256")):
                return dict(record["validation"])
        validation = validator(absolute, **validator_kwargs)
        if not validation.get("sha256"):
            validation["sha256"] = sha256_file(absolute)
        completed_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.data["artifacts"][key] = {
                "path": absolute, "identity": identity, "validator_key": validator_key,
                "input_signature": input_signature,
                "settings_signature": settings_signature,
                "output_fingerprint": validation["sha256"],
                "validation": validation, "validation_result": "passed",
                "completed_at": completed_at,
            }
            if self.enabled:
                _atomic_json(self.path, self.data)
        return dict(validation)


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


def normalize_content_text(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    return "".join(char.casefold() for char in text if char.isalnum())


def validate_srt(path, audio_duration=None, expected_text=None):
    text = _read_text(path).replace("\r\n", "\n")
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    if not blocks:
        raise ArtifactValidationError("SRT has no cues")
    previous_end = -1.0
    last_end = 0.0
    cue_texts = []
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
        cue_texts.append("".join(lines[2:]))
        previous_end = end
        last_end = end
    if audio_duration is not None and last_end > float(audio_duration) + 1.0:
        raise ArtifactValidationError(
            f"SRT ends at {last_end:.3f}s after audio ends at {audio_duration:.3f}s"
        )
    if expected_text is not None:
        expected = normalize_content_text(expected_text)
        actual = normalize_content_text("".join(cue_texts))
        if not expected or actual != expected:
            raise ArtifactValidationError(
                "SRT/CleanText content coverage mismatch "
                f"(expected={stable_signature(expected)[:12]}, actual={stable_signature(actual)[:12]})"
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
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise ArtifactValidationError(f"ffprobe failed: {result.stderr.strip()[-500:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ArtifactValidationError("ffprobe returned invalid JSON") from error


def validate_video(path, audio_duration=None, expected_resolution=None,
                   expected_video_codec=None, expected_audio_codec=None):
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
    video = video_streams[0]
    audio = audio_streams[0]
    if expected_resolution and (int(video.get("width") or 0), int(video.get("height") or 0)) != tuple(expected_resolution):
        raise ArtifactValidationError("MP4 resolution does not match configured output")
    if expected_video_codec and video.get("codec_name") != expected_video_codec:
        raise ArtifactValidationError("MP4 video codec does not match configured output")
    if expected_audio_codec and audio.get("codec_name") != expected_audio_codec:
        raise ArtifactValidationError("MP4 audio codec does not match configured output")
    return {
        "bytes": os.path.getsize(path), "duration_seconds": round(duration, 3),
        "width": video.get("width"), "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "sha256": sha256_file(path),
    }


def validate_stage(stage, path, workspace_dir=None, chapter=None, book_title=None, settings=None):
    settings = settings or {}
    if not os.path.exists(path):
        raise ArtifactValidationError(f"required output is missing: {path}")
    if stage == "crawler":
        return validate_text(path, clean=False)
    if stage == "cleaner":
        return validate_text(path, clean=True)
    if stage == "tts":
        return validate_wav(path)
    if stage in {"subtitle", "video"} and not workspace_dir:
        raise ArtifactValidationError(f"workspace_dir is required for {stage} validation")
    if stage == "subtitle":
        wav_path = os.path.join(workspace_dir, "Audio", f"{book_title}_chapter_{chapter}.wav")
        duration = validate_wav(wav_path)["duration_seconds"] if os.path.exists(wav_path) else None
        clean_path = os.path.join(workspace_dir, "CleanText", f"{book_title}_chapter_{chapter}_clean.txt")
        expected_text = _read_text(clean_path) if os.path.exists(clean_path) else None
        return validate_srt(path, duration, expected_text=expected_text)
    if stage == "image":
        size = settings.get("size") or settings.get("resolution") or (1280, 720)
        return validate_image(path, expected_size=tuple(size))
    if stage == "video":
        wav_path = os.path.join(workspace_dir, "Audio", f"{book_title}_chapter_{chapter}.wav")
        duration = validate_wav(wav_path)["duration_seconds"] if os.path.exists(wav_path) else None
        resolution = settings.get("resolution") or [1280, 720]
        return validate_video(
            path, duration, expected_resolution=tuple(resolution) if resolution else None,
            expected_video_codec=settings.get("video_codec"),
            expected_audio_codec=settings.get("audio_codec"),
        )
    raise ValueError(f"unknown stage: {stage}")


def validate_worker_manifest(path, expected_worker_id=None, expected_chapters=None, confirmed_missing=None):
    """Validate that the lightweight worker duration manifest is valid, non-empty, and complete."""
    if not os.path.exists(path):
        raise ArtifactValidationError(f"manifest file does not exist: {path}")
    if os.path.getsize(path) < 10:
        raise ArtifactValidationError(f"manifest file is too small: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        raise ArtifactValidationError(f"manifest JSON cannot be decoded: {error}") from error

    if not isinstance(data, dict):
        raise ArtifactValidationError("manifest root must be a JSON object")
    if expected_worker_id is not None and int(data.get("worker_id", -1)) != int(expected_worker_id):
        raise ArtifactValidationError(
            f"manifest worker_id mismatch: expected {expected_worker_id}, got {data.get('worker_id')}"
        )
    chapters = data.get("chapters")
    if not isinstance(chapters, list):
        raise ArtifactValidationError("manifest chapters must be a list")

    confirmed_missing = {int(c) for c in (confirmed_missing or data.get("source_missing", []))}
    recorded_missing = {int(c) for c in data.get("source_missing", [])}

    chapter_nums = []
    total_duration = 0.0
    for pos, item in enumerate(chapters, start=1):
        if not isinstance(item, dict):
            raise ArtifactValidationError(f"manifest chapter #{pos} is not an object")
        num = item.get("chap_num")
        if num is None or not str(num).isdigit():
            raise ArtifactValidationError(f"manifest chapter #{pos} has invalid chap_num")
        dur = item.get("dur")
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0.25:
            raise ArtifactValidationError(f"manifest chapter {num} has invalid duration {dur}")
        chapter_nums.append(int(num))
        total_duration += dur

    if expected_chapters is not None:
        expected_set = {int(c) for c in expected_chapters}
        covered_set = set(chapter_nums) | confirmed_missing
        missing_set = expected_set - covered_set
        extra_set = set(chapter_nums) - expected_set
        if missing_set or extra_set:
            raise ArtifactValidationError(
                f"manifest chapters mismatch: missing={sorted(missing_set)}, unexpected={sorted(extra_set)}"
            )

    return {
        "bytes": os.path.getsize(path),
        "worker_id": data.get("worker_id"),
        "chapter_count": len(chapters),
        "missing_count": len(recorded_missing),
        "total_duration_seconds": round(total_duration, 3),
        "sha256": sha256_file(path),
    }
