import os
import yaml
import logging
import subprocess
import asyncio
import edge_tts
import shutil

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
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    local_path = r"C:\Users\cyt18\anaconda3\Library\bin\ffmpeg.exe"
    if os.path.exists(local_path):
        return local_path
    return "ffmpeg"


async def _generate_part_async(semaphore, text, mp3_path, part_label, voice, max_retries=3):
    """
    非同步生成單一段落的 Edge-TTS MP3，使用 Semaphore 控制最大並行數。
    若失敗則重試最多 max_retries 次。
    """
    async with semaphore:
        for attempt in range(max_retries):
            try:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(mp3_path)
                logging.info(f"[TTS_MS] ✓ {part_label} (asyncio)")
                return True
            except Exception as e:
                logging.warning(f"[TTS_MS] {part_label} attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5)
        logging.error(f"[TTS_MS] ✗ {part_label} 全部重試失敗，跳過")
        return False


async def _process_chapter_async(lines, book_title, chap_num, audio_dir, voice, ffmpeg_path, max_concurrency=5):
    """
    並行非同步處理一章的所有段落，最多同時 max_concurrency 個 Edge-TTS 請求。
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    # 建立所有段落的任務
    tasks = []
    task_meta = []  # [(part_idx, text, mp3_path, wav_path)]
    for part_idx, line in enumerate(lines):
        text = line.strip()
        if not text:
            continue
        audio_filename = f"{book_title}_chapter_{chap_num}_tmp_part_{part_idx+1:03d}.wav"
        mp3_filename   = f"{book_title}_chapter_{chap_num}_tmp_part_{part_idx+1:03d}.mp3"
        wav_path = os.path.join(audio_dir, audio_filename)
        mp3_path = os.path.join(audio_dir, mp3_filename)
        part_label = f"[{part_idx+1}/{len(lines)}] {audio_filename}"

        task_meta.append((part_idx, text, mp3_path, wav_path))

        if os.path.exists(wav_path):
            logging.info(f"[TTS_MS] Resuming existing part: {audio_filename}")
            tasks.append(asyncio.coroutine(lambda: True)() if False else asyncio.sleep(0))
            # 已存在的 part 用 placeholder 填充（不重新生成）
        else:
            tasks.append(_generate_part_async(semaphore, text, mp3_path, part_label, voice))

    # 並行執行所有 TTS（已存在的 part 直接回傳 None）
    results_raw = []
    for i, (part_idx, text, mp3_path, wav_path) in enumerate(task_meta):
        if os.path.exists(wav_path):
            results_raw.append(None)   # 已存在，不需 TTS
        else:
            results_raw.append(tasks[i])

    # 分批執行（只對尚未存在的段落發起 TTS）
    pending_indices = [i for i, v in enumerate(results_raw) if v is not None]
    pending_coros = [results_raw[i] for i in pending_indices]

    if pending_coros:
        logging.info(f"[TTS_MS] 並行生成 {len(pending_coros)} 段語音 (最大並行 {max_concurrency})...")
        await asyncio.gather(*pending_coros)

    # TTS 完成後，將 MP3 轉換成 WAV（逐一執行 ffmpeg，但通常很快）
    generated_parts = []
    valid_lines = []
    for part_idx, text, mp3_path, wav_path in task_meta:
        if os.path.exists(wav_path):
            # 已是完整 WAV，直接加入
            generated_parts.append(wav_path)
            valid_lines.append(text)
        elif os.path.exists(mp3_path):
            try:
                subprocess.run(
                    [ffmpeg_path, "-y", "-i", mp3_path, wav_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                if os.path.exists(mp3_path):
                    os.remove(mp3_path)
                generated_parts.append(wav_path)
                valid_lines.append(text)
            except Exception as e:
                audio_filename = os.path.basename(wav_path)
                logging.error(f"[TTS_MS] ✗ MP3→WAV 轉換失敗 {audio_filename}: {e}")
                try:
                    os.remove(mp3_path)
                except Exception:
                    pass
        else:
            audio_filename = os.path.basename(wav_path)
            logging.warning(f"[TTS_MS] 段落 {audio_filename} 未生成（TTS 失敗），跳過")

    return generated_parts, valid_lines


def run_tts_ms():
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
        return

    # 取得語音設定（從 config.yaml 或預設值）
    tts_cfg = config.get('tts', {})
    voice = tts_cfg.get('edge_voice', tts_cfg.get('voice', 'zh-CN-YunxiNeural'))
    max_concurrency = int(tts_cfg.get('tts_concurrency', 5))

    filenames = sorted([f for f in os.listdir(clean_text_dir) if f.endswith("_clean.txt")])

    for filename in filenames:
        clean_path = os.path.join(clean_text_dir, filename)
        with open(clean_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if not lines:
            continue

        # 解析章節號碼
        parts = filename.split("_")
        chap_num = "1"
        for i, p in enumerate(parts):
            if p == "chapter" and i + 1 < len(parts):
                chap_num = parts[i + 1]
                break

        # 最終輸出：{書名}_chapter_{N}.wav（無 part）
        wav_filename = f"{book_title}_chapter_{chap_num}.wav"
        wav_path = os.path.join(audio_dir, wav_filename)

        if os.path.exists(wav_path):
            logging.info(f"[TTS_MS] Skipping existing: {wav_filename}")
            continue

        logging.info(f"[TTS_MS] Processing {filename} ({len(lines)} segments, 並行={max_concurrency})...")

        # ⚡ 非同步並行 TTS
        generated_parts, valid_lines = asyncio.run(
            _process_chapter_async(lines, book_title, chap_num, audio_dir, voice, ffmpeg_path, max_concurrency)
        )

        # 所有 part 產生完後，合併成單一 chapter WAV
        if generated_parts:
            # 產生 SRT
            try:
                from subtitle_gen import generate_chapter_srt
                subtitles_dir = os.path.join(workspace_dir, "Subtitles")
                if not os.path.exists(subtitles_dir):
                    os.makedirs(subtitles_dir)
                srt_path = os.path.join(subtitles_dir, f"{book_title}_chapter_{chap_num}.srt")
                generate_chapter_srt(generated_parts, valid_lines, srt_path)
            except Exception as e:
                logging.error(f"[TTS_MS] Failed to generate SRT for chapter {chap_num}: {e}")

            # 合併 WAV
            if len(generated_parts) == 1:
                shutil.move(generated_parts[0], wav_path)
                logging.info(f"[TTS_MS] ✓ Renamed single part to: {wav_filename}")
            else:
                concat_list_path = wav_path + "_concat.txt"
                with open(concat_list_path, "w", encoding="utf-8") as f:
                    for p in generated_parts:
                        safe_path = p.replace("\\", "/")
                        f.write(f"file '{safe_path}'\n")
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", wav_path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    logging.info(f"[TTS_MS] ✓ Merged {len(generated_parts)} parts -> {wav_filename}")
                    # 刪除 part 檔
                    for p in generated_parts:
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                except Exception as e:
                    logging.error(f"[TTS_MS] ✗ Merge failed for {wav_filename}: {e}")
                finally:
                    if os.path.exists(concat_list_path):
                        try:
                            os.remove(concat_list_path)
                        except Exception:
                            pass
        else:
            logging.warning(f"[TTS_MS] No parts generated for chapter {chap_num}, skipping merge.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_tts_ms()
