import os
import yaml
import logging
import subprocess
import asyncio
import edge_tts
import shutil
import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from opencc import OpenCC
from pydub import AudioSegment
from pydub.silence import detect_leading_silence
try:
    from .artifact_validation import (ArtifactValidationError, stable_signature,
                                      validate_srt, validate_wav)
except ImportError:
    from artifact_validation import ArtifactValidationError, stable_signature, validate_srt, validate_wav

# Spyder/IPython 的 kernel 已有執行中的 event loop，
# 需要 nest_asyncio 才能在其中再次呼叫 asyncio.run()
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass  # 非 IPython 環境時不影響


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_ffmpeg_path():
    env_path = os.environ.get("FFMPEG_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    return "ffmpeg"


import re


_T2S = OpenCC("t2s")
_TTS_PROSODY_REPLACEMENTS = (
    ("鼠標敲打著鍵盤", "鼠標，敲打著鍵盤"),
)
_TERMINAL_RE = re.compile(r"(?:……|\.\.\.|[。！？!?…])[”’」』）》）)]*$")
_COMMA_RE = re.compile(r"[，,][”’」』）》）)]*$")
_SEMICOLON_RE = re.compile(r"[；;：:][”’」』）》）)]*$")


def speech_text_for_voice(display_text, voice):
    """Return TTS-only text; Cleaner/subtitle text remains Traditional."""
    speech_text = str(display_text or "")
    for source, replacement in _TTS_PROSODY_REPLACEMENTS:
        speech_text = speech_text.replace(source, replacement)
    if str(voice or "").startswith("zh-CN-"):
        speech_text = _T2S.convert(speech_text)
    return speech_text


def _pause_after(text, paragraph_end=False):
    if paragraph_end:
        return 600
    if _TERMINAL_RE.search(text):
        return 340
    if _SEMICOLON_RE.search(text):
        return 210
    if _COMMA_RE.search(text):
        return 130
    return 45


def _segment_lines_with_paragraphs(lines):
    """Return non-empty lines and whether a blank-line paragraph follows."""
    result = []
    for index, raw in enumerate(lines):
        text = raw.strip()
        if not text:
            continue
        next_nonempty = None
        for following in range(index + 1, len(lines)):
            if lines[following].strip():
                next_nonempty = following
                break
        paragraph_end = next_nonempty is None or any(
            not lines[pos].strip() for pos in range(index + 1, next_nonempty)
        )
        result.append((text, paragraph_end))
    return result


def _trim_and_add_pause(wav_path, text, paragraph_end=False):
    """Normalize Edge-TTS boundary silence and append a graded pause."""
    audio = AudioSegment.from_wav(wav_path)
    threshold = -45.0
    leading = detect_leading_silence(audio, silence_threshold=threshold, chunk_size=5)
    trailing = detect_leading_silence(audio.reverse(), silence_threshold=threshold, chunk_size=5)
    trim_left = max(0, leading - 35)
    trim_right = max(0, trailing - 60)
    end = max(trim_left + 1, len(audio) - trim_right)
    trimmed = audio[trim_left:end]
    normalized = trimmed + AudioSegment.silent(
        duration=_pause_after(text, paragraph_end), frame_rate=trimmed.frame_rate,
    )
    normalized.export(wav_path, format="wav")


def sanitize_text(text):
    if not text:
        return ""
    # 移除不可見與控制字元 (Unicode zero-width spaces, BOM, control characters)
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\x00-\x1f\x7f]', '', text)
    # 將可能干擾 SSML / XML 的符號替換為全形符號
    text = text.replace('<', '＜').replace('>', '＞').replace('&', '＆')
    return text.strip()


def segment_cache_key(text, voice, rate, settings_signature=""):
    speech = sanitize_text(speech_text_for_voice(text, voice))
    payload = {"speech_text": speech, "voice": voice, "rate": rate,
               "settings_signature": settings_signature, "version": "tts-segment-v3"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _semantic_subsegments(text, max_chars=80):
    pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?；;，,])", text) if piece.strip()]
    result = []
    for piece in pieces or [text]:
        result.extend(piece[i:i + max_chars] for i in range(0, len(piece), max_chars))
    return [piece for piece in result if piece]


def create_silent_wav(wav_path, ffmpeg_path="ffmpeg", duration=1.5):
    """當某段落連跑 3 次均因微軟敏感詞審查失敗時，自動生成 1.5s 靜音 WAV 墊檔。"""
    try:
        cmd = [
            ffmpeg_path, "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=24000:cl=mono",
            "-t", str(duration),
            wav_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(wav_path) and os.path.getsize(wav_path) > 100
    except Exception as e:
        logging.error(f"[TTS_MS] 生成靜音 WAV 墊檔失敗: {e}")
        return False


async def _generate_one_segment(semaphore, text, mp3_path, wav_path, part_label, voice,
                                rate="+0%", ffmpeg_path="ffmpeg", max_retries=5,
                                normalized_retries=3, split_retries=3,
                                silent_fallback=True, silent_duration=1.5,
                                fallback_records=None, chapter=None, segment_index=None):
    """
    非同步生成單一段落的 Edge-TTS MP3。
    使用 Semaphore 限制最大並行數，失敗時最多重試 max_retries 次。
    若全部失敗（如觸發微軟敏感詞過濾），自動改用 1.5s 靜音 WAV 墊檔容錯。
    """
    original_speech = speech_text_for_voice(text, voice)
    clean_t = sanitize_text(original_speech)
    attempts = []

    async with semaphore:
        async def attempt_text(candidate, count, stage, target_mp3):
            last_error = None
            for attempt in range(max(0, int(count))):
                try:
                    communicate = edge_tts.Communicate(candidate, voice, rate=rate)
                    await communicate.save(target_mp3)
                    if os.path.exists(target_mp3) and os.path.getsize(target_mp3) > 100:
                        attempts.append({"stage": stage, "attempt": attempt + 1, "success": True})
                        return True, None
                    raise ValueError("MP3 output is empty or too small")
                except Exception as error:
                    last_error = error
                    attempts.append({"stage": stage, "attempt": attempt + 1,
                                     "success": False, "reason": str(error)[:300]})
                    if os.path.exists(target_mp3):
                        try: os.remove(target_mp3)
                        except OSError: pass
                    if attempt + 1 < count:
                        await asyncio.sleep(2.0)
            return False, last_error

        ok, last_error = await attempt_text(original_speech, max_retries, "original", mp3_path)
        if not ok and clean_t:
            ok, last_error = await attempt_text(clean_t, normalized_retries, "normalized", mp3_path)
        if not ok and clean_t:
            split_audio = AudioSegment.empty()
            split_ok = True
            for sub_index, subtext in enumerate(_semantic_subsegments(clean_t), 1):
                sub_mp3 = f"{mp3_path}.split-{sub_index}.mp3"
                sub_ok, last_error = await attempt_text(subtext, split_retries, "split", sub_mp3)
                if not sub_ok:
                    split_ok = False
                    break
                split_audio += AudioSegment.from_file(sub_mp3, format="mp3")
                os.remove(sub_mp3)
            if split_ok and len(split_audio) > 0:
                split_audio.export(wav_path, format="wav")
                return True
        if ok:
            logging.info(f"[TTS_MS] ✓ {part_label}")
            return True
        if silent_fallback and create_silent_wav(wav_path, ffmpeg_path, duration=float(silent_duration)):
            record = {
                "silent_fallback_used": True, "chapter": chapter,
                "segment_index": segment_index,
                "original_text_hash": hashlib.sha256(str(text).encode("utf-8")).hexdigest(),
                "failure_reason": str(last_error)[:500], "attempts": attempts,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            if fallback_records is not None:
                fallback_records.append(record)
            logging.warning("[TTS_MS] %s 使用有限 Silent Fallback", part_label)
            return True
        return False


async def _process_chapter_async(lines, book_title, chap_num, audio_dir, voice, rate,
                                  ffmpeg_path, max_concurrency=5, max_retries=5,
                                  normalized_retries=3, split_retries=3,
                                  silent_fallback=True, silent_duration=1.5,
                                  settings_signature="", fallback_records=None):
    """
    並行非同步處理一章所有段落。
    - 最多同時 max_concurrency 個 Edge-TTS 並行請求。
    - 若任何段落最終失敗，回傳 ([], [])，整章廢棄。
    - 回傳 (generated_parts, valid_lines) 表示全部成功。
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    # 整理每個段落的中繼資訊，保留 Cleaner 的空白行作為段落邊界。
    task_metas = []
    cache_dir = os.path.join(audio_dir, "SegmentCache")
    os.makedirs(cache_dir, exist_ok=True)
    segment_lines = _segment_lines_with_paragraphs(lines)
    for part_idx, (text, paragraph_end) in enumerate(segment_lines):
        cache_key = segment_cache_key(text, voice, rate, settings_signature)
        wav_path = os.path.join(cache_dir, cache_key + ".wav")
        mp3_path = os.path.join(cache_dir, cache_key + ".mp3")
        part_label = f"[Ch{chap_num} 段落 {part_idx+1}/{len(segment_lines)}]"
        task_metas.append((text, paragraph_end, mp3_path, wav_path, part_label))

    if not task_metas:
        logging.warning(f"[TTS_MS] 第 {chap_num} 章沒有有效文字段落")
        return [], []

    # 建立並行任務（已有 WAV 的直接跳過 TTS）
    coros = []
    skip_flags = []
    for text, paragraph_end, mp3_path, wav_path, part_label in task_metas:
        cache_valid = False
        if os.path.exists(wav_path):
            try:
                validate_wav(wav_path)
                cache_valid = True
            except (ArtifactValidationError, OSError, ValueError):
                cache_valid = False
        if cache_valid:
            logging.info(f"[TTS_MS] Resuming validated content cache: {os.path.basename(wav_path)}")
            coros.append(None)
            skip_flags.append(True)
        else:
            # 清除殘留的不完整 WAV
            if os.path.exists(wav_path):
                os.remove(wav_path)
            coros.append(_generate_one_segment(
                semaphore, text, mp3_path, wav_path, part_label, voice,
                rate, ffmpeg_path, max_retries, normalized_retries, split_retries,
                silent_fallback, silent_duration, fallback_records, chap_num,
                len(skip_flags) + 1,
            ))
            skip_flags.append(False)

    # 並行執行所有 TTS 請求
    pending_coros = [(i, c) for i, (c, skip) in enumerate(zip(coros, skip_flags)) if not skip]
    if pending_coros:
        logging.info(f"[TTS_MS] 第 {chap_num} 章：並行生成 {len(pending_coros)} 段語音 (並行={max_concurrency})")
        await asyncio.gather(*[c for _, c in pending_coros])

    # 將本輪成功的 MP3 轉為 WAV，並保留已成功的段落 WAV 快取
    for i, (text, paragraph_end, mp3_path, wav_path, part_label) in enumerate(task_metas):
        if not skip_flags[i] and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [ffmpeg_path, "-y", "-i", mp3_path, wav_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
                    await asyncio.to_thread(
                        _trim_and_add_pause, wav_path, text, paragraph_end,
                    )
                    os.remove(mp3_path)
            except Exception as e:
                logging.error(f"[TTS_MS] ✗ {part_label} MP3→WAV 轉換失敗: {e}")

    # 檢查是否所有段落的 WAV 均已備齊
    generated_parts = []
    valid_lines = []
    missing_count = 0

    for text, paragraph_end, mp3_path, wav_path, part_label in task_metas:
        try:
            validate_wav(wav_path)
            generated_parts.append(wav_path)
            valid_lines.append(text)
        except (ArtifactValidationError, OSError, ValueError):
            missing_count += 1

    if missing_count > 0:
        logging.warning(
            f"[TTS_MS] ⚠️ 第 {chap_num} 章有 {missing_count} 個段落未完成 TTS。"
            f"已成功的 {len(generated_parts)} 段已保留 WAV 快取，下一次重試將僅重派失敗段落。"
        )
        return [], []

    return generated_parts, valid_lines


def run_tts_ms(target_indices=None):
    """
    回傳兩個 set：
      - succeeded_chapters: 成功生成 WAV 的章節號碼集合
      - failed_chapters:    最終失敗的章節號碼集合
    """
    config = load_config()
    book_title = config['book_title']

    workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", config['paths']['workspace_base'], book_title)
    clean_text_dir = os.path.join(workspace_dir, "CleanText")
    audio_dir = os.path.join(workspace_dir, "Audio")
    ffmpeg_path = get_ffmpeg_path()

    if not os.path.exists(audio_dir):
        os.makedirs(audio_dir)

    if not os.path.exists(clean_text_dir):
        logging.error(f"[TTS_MS] CleanText directory not found: {clean_text_dir}")
        return set(), set()

    # 取得語音設定
    tts_cfg = config.get('tts', {})
    voice = tts_cfg.get('edge_voice', tts_cfg.get('voice', 'zh-CN-YunjianNeural'))
    # Legacy configs have no edge_rate and must keep their original speed.
    # New GUI runs receive +25% from catalog_parser.py.
    rate = tts_cfg.get('edge_rate', '+0%')
    max_concurrency = int(tts_cfg.get('concurrency', tts_cfg.get('tts_concurrency', 5)))
    max_retries = int(tts_cfg.get('segment_retries', tts_cfg.get('tts_max_retries', 5)))
    normalized_retries = int(tts_cfg.get('normalized_retries', 3))
    split_retries = int(tts_cfg.get('split_retries', 3))
    chapter_retries = int(tts_cfg.get('chapter_retries', 3))
    fallback_cfg = tts_cfg.get('silent_fallback') or {}
    fallback_enabled = fallback_cfg.get('enabled', True)
    fallback_duration = float(fallback_cfg.get('duration_seconds', 1.5))
    settings_signature = stable_signature({"version": "tts-v5-content-cache", **tts_cfg})
    fallback_records = []
    logging.info("[TTS_MS] Edge-TTS voice=%s, rate=%s", voice, rate)

    filenames = sorted([f for f in os.listdir(clean_text_dir) if f.endswith("_clean.txt")])

    succeeded_chapters = set()
    failed_chapters = set()

    for filename in filenames:
        # 解析章節號碼
        parts = filename.split("_")
        chap_num = "1"
        for i, p in enumerate(parts):
            if p == "chapter" and i + 1 < len(parts):
                chap_num = parts[i + 1]
                break

        if target_indices is not None and int(chap_num) not in target_indices:
            continue

        clean_path = os.path.join(clean_text_dir, filename)
        with open(clean_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if not lines:
            continue

        # 最終輸出：{書名}_chapter_{N}.wav
        wav_filename = f"{book_title}_chapter_{chap_num}.wav"
        wav_path = os.path.join(audio_dir, wav_filename)
        srt_path = os.path.join(workspace_dir, "Subtitles", f"{book_title}_chapter_{chap_num}.srt")

        try:
            wav_validation = validate_wav(wav_path)
            validate_srt(srt_path, wav_validation["duration_seconds"], expected_text="".join(lines))
            wav_ok = srt_ok = True
        except (ArtifactValidationError, OSError, ValueError):
            wav_ok = srt_ok = False
        if wav_ok and srt_ok:
            logging.info(f"[TTS_MS] Skipping existing WAV + SRT: {wav_filename}")
            succeeded_chapters.add(int(chap_num))
            continue
        if wav_ok != srt_ok:
            logging.warning("[TTS_MS] 第 %s 章只有部分產物，清除後安全重建 WAV + SRT。", chap_num)
            for incomplete_path in (wav_path, srt_path):
                if os.path.exists(incomplete_path):
                    os.remove(incomplete_path)

        # ── 章節層級重試：最多嘗試 3 次（每次都是全章從頭重做）──
        CHAPTER_MAX_ATTEMPTS = chapter_retries
        chapter_success = False

        for chapter_attempt in range(1, CHAPTER_MAX_ATTEMPTS + 1):
            if chapter_attempt > 1:
                logging.warning(
                    f"[TTS_MS] ↻ 第 {chap_num} 章 第 {chapter_attempt}/{CHAPTER_MAX_ATTEMPTS} 次增量重試..."
                )

            logging.info(
                f"[TTS_MS] ▶ 第 {chap_num} 章 嘗試 {chapter_attempt}/{CHAPTER_MAX_ATTEMPTS} "
                f"({len([l for l in lines if l.strip()])} 段，並行={max_concurrency})"
            )

            # ── Step 1: 非同步並行 TTS ──
            generated_parts, valid_lines = asyncio.run(
                _process_chapter_async(
                    lines, book_title, chap_num, audio_dir,
                    voice, rate, ffmpeg_path, max_concurrency, max_retries,
                    normalized_retries, split_retries, fallback_enabled,
                    fallback_duration, settings_signature, fallback_records,
                )
            )

            if not generated_parts:
                logging.error(f"[TTS_MS] ✗ 第 {chap_num} 章嘗試 {chapter_attempt} TTS 失敗")
                continue  # 進入下一次章節重試

            # ── Step 2: 生成 SRT 字幕 ──
            srt_ok = False
            try:
                from subtitle_gen import generate_chapter_srt
                subtitles_dir = os.path.join(workspace_dir, "Subtitles")
                os.makedirs(subtitles_dir, exist_ok=True)
                srt_path = os.path.join(subtitles_dir, f"{book_title}_chapter_{chap_num}.srt")
                generate_chapter_srt(generated_parts, valid_lines, srt_path)
                validate_srt(srt_path, expected_text="".join(valid_lines))
                srt_ok = True
            except Exception as e:
                logging.error(f"[TTS_MS] ✗ 第 {chap_num} 章嘗試 {chapter_attempt} SRT 生成失敗: {e}")

            if not srt_ok:
                continue  # 進入下一次章節重試

            # ── Step 3: 合併 WAV ──
            merge_ok = False
            partial_wav_path = wav_path + ".tmp.wav"
            if os.path.exists(partial_wav_path):
                os.remove(partial_wav_path)
            if len(generated_parts) == 1:
                shutil.copy2(generated_parts[0], partial_wav_path)
                try:
                    validate_wav(partial_wav_path)
                    os.replace(partial_wav_path, wav_path)
                    logging.info(f"[TTS_MS] ✓ 第 {chap_num} 章 WAV 完成 (單段直接使用)")
                    merge_ok = True
                except (ArtifactValidationError, OSError, ValueError):
                    merge_ok = False
            else:
                concat_list_path = wav_path + "_concat.txt"
                with open(concat_list_path, "w", encoding="utf-8") as f:
                    for p in generated_parts:
                        safe_path = p.replace("\\", "/").replace("'", "'\\''")
                        f.write(f"file '{safe_path}'\n")
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                         "-i", concat_list_path, "-c", "copy", partial_wav_path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    try:
                        validate_wav(partial_wav_path)
                        os.replace(partial_wav_path, wav_path)
                        logging.info(
                            f"[TTS_MS] ✓ 第 {chap_num} 章 WAV 合併完成 ({len(generated_parts)} 段)"
                        )
                        merge_ok = True
                    except (ArtifactValidationError, OSError, ValueError) as error:
                        raise ValueError(f"合併後 WAV 驗證失敗: {error}") from error
                except Exception as e:
                    logging.error(
                        f"[TTS_MS] ✗ 第 {chap_num} 章嘗試 {chapter_attempt} WAV 合併失敗: {e}"
                    )
                finally:
                    if os.path.exists(concat_list_path):
                        try:
                            os.remove(concat_list_path)
                        except Exception:
                            pass

            if not merge_ok:
                if os.path.exists(partial_wav_path):
                    try:
                        os.remove(partial_wav_path)
                    except Exception:
                        pass
                if os.path.exists(wav_path):
                    try:
                        os.remove(wav_path)
                    except Exception:
                        pass
                continue  # 進入下一次章節重試

            # 三步全部通過 ✅
            chapter_success = True
            logging.info(
                f"[TTS_MS] ✅ 第 {chap_num} 章完成 "
                f"(嘗試 {chapter_attempt}/{CHAPTER_MAX_ATTEMPTS}，WAV + SRT 齊全)"
            )
            break  # 成功，跳出章節重試迴圈

        if chapter_success:
            succeeded_chapters.add(int(chap_num))
        else:
            logging.error(
                f"[TTS_MS] ❌ 第 {chap_num} 章經過 {CHAPTER_MAX_ATTEMPTS} 次完整嘗試仍失敗，放棄！"
            )
            # 清理該章所有殘留的暫存 part 檔
            import glob as _glob
            stale_parts = _glob.glob(os.path.join(
                audio_dir, f"{book_title}_chapter_{chap_num}_tmp_part_*"
            ))
            for sp in stale_parts:
                try:
                    os.remove(sp)
                except Exception:
                    pass
            failed_chapters.add(int(chap_num))
            raise RuntimeError(
                f"[TTS_MS] 第 {chap_num} 章經過 {CHAPTER_MAX_ATTEMPTS} 次完整嘗試仍失敗，流程中止"
            )

    if fallback_records:
        manifest_path = os.path.join(audio_dir, "silent-fallback-manifest.json")
        temporary = manifest_path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "count": len(fallback_records),
                       "segments": fallback_records}, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, manifest_path)
        logging.warning("[TTS_MS] 本次共有 %s 個 TTS Segment 使用 Silent Fallback", len(fallback_records))
    return succeeded_chapters, failed_chapters


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    succeeded, failed = run_tts_ms()
    if failed:
        logging.error(f"[TTS_MS] === 最終失敗章節 (共 {len(failed)} 章): {sorted(failed)} ===")
    else:
        logging.info(f"[TTS_MS] === 全部 {len(succeeded)} 章 TTS 成功 ===")
