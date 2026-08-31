"""Generate an isolated punctuation-aware Edge-TTS audiobook preview.

This experiment intentionally does not use the production 18-character
Cleaner.  Every emitted line ends at source punctuation; no word is split to
meet a character limit.  Each line is still synthesized independently so we
can evaluate whether trimming TTS boundary silence and adding graded pauses is
good enough before changing the production pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import edge_tts
import imageio_ffmpeg
from opencc import OpenCC
from pydub import AudioSegment
from pydub.silence import detect_leading_silence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cleaner import clean_text_content  # noqa: E402
from src.crawler import fetch_chapter_text  # noqa: E402


DEFAULT_URL = "https://tw.hjwzw.com/Book/Read/24632,3184884"
DEFAULT_VOICE = "zh-CN-YunxiNeural"
CLAUSE_RE = re.compile(r".*?(?:……|\.\.\.|[，,；;：:。！？!?…]+)[”’」』）》）)]*|.+$", re.S)
TERMINAL_RE = re.compile(r"(?:……|\.\.\.|[。！？!?…])[”’」』）》）)]*$")
COMMA_RE = re.compile(r"[，,][”’」』）》）)]*$")
SEMICOLON_RE = re.compile(r"[；;：:][”’」』）》）)]*$")
SEMANTIC_TURN_RE = re.compile(r"卻|但|然而|不過|只是|所以|因此")
T2S = OpenCC("t2s")

# These hints exist only in the string sent to speech synthesis. They never
# modify Cleaner output, display text, or subtitles.
TTS_PROSODY_REPLACEMENTS = (
    ("鼠標敲打著鍵盤", "鼠標，敲打著鍵盤"),
)


def speech_text_for_voice(display_text: str, voice: str) -> str:
    """Build model-friendly speech text without changing subtitle text."""
    speech_text = display_text
    for source, replacement in TTS_PROSODY_REPLACEMENTS:
        speech_text = speech_text.replace(source, replacement)
    if voice.startswith("zh-CN-"):
        speech_text = T2S.convert(speech_text)
    return speech_text


def split_semantic_turn(clause: str) -> list[str]:
    """Add a breathing boundary before a late turn word in a long clause.

    This is deliberately narrow: unlike the production Cleaner it never cuts
    at a character midpoint.  Example: ``但在……過程中卻沒有……。`` becomes
    ``但在……過程中，`` + ``卻沒有……。``.
    """
    if len(clause) < 20:
        return [clause]
    for match in SEMANTIC_TURN_RE.finditer(clause):
        index = match.start()
        if index >= 10 and len(clause) - index >= 5:
            left = clause[:index].rstrip("，,") + "，"
            right = clause[index:].lstrip()
            return [left, right]
    return [clause]


def punctuation_aware_segments(text: str) -> list[dict[str, object]]:
    """Split only at source punctuation, retaining paragraph information."""
    results: list[dict[str, object]] = []
    paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    for paragraph_index, paragraph in enumerate(paragraphs):
        raw_clauses = []
        for match in CLAUSE_RE.finditer(paragraph):
            raw_clauses.extend(split_semantic_turn(match.group(0).strip()))
        clauses: list[str] = []
        pending = ""
        for clause in raw_clauses:
            if not clause:
                continue
            pending += clause
            # Tiny comma fragments such as “卡卡卡，” remain attached to the
            # following phrase rather than becoming their own TTS request.
            visible_len = len(re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", pending))
            if COMMA_RE.search(pending) and visible_len <= 6:
                continue
            clauses.append(pending)
            pending = ""
        if pending:
            if clauses:
                clauses[-1] += pending
            else:
                clauses.append(pending)

        for clause_index, clause in enumerate(clauses):
            paragraph_end = clause_index == len(clauses) - 1
            results.append(
                {
                    "text": clause,
                    "paragraph": paragraph_index + 1,
                    "paragraph_end": paragraph_end,
                    "pause_ms": pause_after(clause, paragraph_end),
                }
            )
    return results


def pause_after(text: str, paragraph_end: bool) -> int:
    if paragraph_end:
        return 600
    if TERMINAL_RE.search(text):
        return 340
    if SEMICOLON_RE.search(text):
        return 210
    if COMMA_RE.search(text):
        return 130
    return 45


def trim_boundary_silence(audio: AudioSegment) -> tuple[AudioSegment, int, int]:
    """Trim only excessive edge silence, preserving safe speech margins."""
    threshold = -45.0
    leading = detect_leading_silence(audio, silence_threshold=threshold, chunk_size=5)
    trailing = detect_leading_silence(audio.reverse(), silence_threshold=threshold, chunk_size=5)
    trim_left = max(0, leading - 35)
    trim_right = max(0, trailing - 60)
    end = max(trim_left + 1, len(audio) - trim_right)
    return audio[trim_left:end], trim_left, trim_right


async def synthesize(segments: list[dict[str, object]], work_dir: Path, voice: str, rate: str) -> None:
    semaphore = asyncio.Semaphore(5)

    async def one(index: int, segment: dict[str, object]) -> None:
        path = work_dir / f"segment_{index:04d}.mp3"
        if path.exists() and path.stat().st_size > 100:
            return
        async with semaphore:
            # `segment["text"]` remains the original Traditional Chinese used
            # by Cleaner/subtitles. Only this transient speech string is
            # converted to Simplified Chinese for zh-CN voices.
            speech_text = speech_text_for_voice(str(segment["text"]), voice)
            await edge_tts.Communicate(speech_text, voice, rate=rate).save(str(path))
        # Do not print source prose: Windows cp950 consoles cannot represent
        # every CJK character, and a progress message must never abort TTS.
        print(f"TTS {index:04d}/{len(segments):04d}", flush=True)

    await asyncio.gather(*(one(i, segment) for i, segment in enumerate(segments, 1)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--rate", default="+50%")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "Workspace" / "ProsodyPreview")
    parser.add_argument(
        "--final-only",
        action="store_true",
        help="After a successful merge, remove all temporary audio and analysis files.",
    )
    args = parser.parse_args()

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    AudioSegment.converter = ffmpeg
    AudioSegment.ffmpeg = ffmpeg
    AudioSegment.ffprobe = ffmpeg

    args.output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = args.output_dir / "parts"
    work_dir.mkdir(exist_ok=True)

    title, raw_text = fetch_chapter_text(args.url, timeout=30)
    cleaned = clean_text_content(raw_text, title, "全職高手")
    segments = punctuation_aware_segments(cleaned)
    if not segments:
        raise RuntimeError("Cleaner did not produce any TTS segments")

    (args.output_dir / "cleaned.txt").write_text(cleaned, encoding="utf-8")
    (args.output_dir / "segments.txt").write_text(
        "\n".join(str(segment["text"]) for segment in segments), encoding="utf-8"
    )
    (args.output_dir / "segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"{title}: {len(segments)} punctuation-aware TTS segments", flush=True)
    asyncio.run(synthesize(segments, work_dir, args.voice, args.rate))

    combined = AudioSegment.empty()
    diagnostics: list[dict[str, object]] = []
    for index, segment in enumerate(segments, 1):
        mp3_path = work_dir / f"segment_{index:04d}.mp3"
        wav_part_path = work_dir / f"segment_{index:04d}.wav"
        if not wav_part_path.exists() or wav_part_path.stat().st_size <= 100:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(mp3_path), str(wav_part_path)],
                check=True,
            )
        audio = AudioSegment.from_wav(wav_part_path)
        trimmed, trim_left, trim_right = trim_boundary_silence(audio)
        pause_ms = int(segment["pause_ms"])
        combined += trimmed + AudioSegment.silent(duration=pause_ms, frame_rate=trimmed.frame_rate)
        diagnostics.append(
            {
                **segment,
                "source_ms": len(audio),
                "trimmed_leading_ms": trim_left,
                "trimmed_trailing_ms": trim_right,
                "spoken_ms": len(trimmed),
            }
        )

    wav_path = args.output_dir / "全職高手_第一章_標點分段_縮短間隔.wav"
    combined.export(wav_path, format="wav")
    (args.output_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.final_only:
        # The final WAV has already been closed and verified by pydub export.
        # Removing the private work directory leaves no hundreds of MP3/WAV
        # chunks behind in the voice output folder.
        shutil.rmtree(work_dir)
        for analysis_name in ("cleaned.txt", "segments.txt", "segments.json", "diagnostics.json"):
            analysis_path = args.output_dir / analysis_name
            if analysis_path.exists():
                analysis_path.unlink()
    print(f"WAV: {wav_path}", flush=True)


if __name__ == "__main__":
    main()
