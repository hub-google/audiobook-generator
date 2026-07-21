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


async def _generate_one_segment(semaphore, text, mp3_path, part_label, voice, max_retries=3):
    """
    非同步生成單一段落的 Edge-TTS MP3。
    使用 Semaphore 限制最大並行數，失敗時最多重試 max_retries 次。
    回傳 True 表示成功，False 表示最終失敗。
    """
    async with semaphore:
        for attempt in range(max_retries):
            try:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(mp3_path)
                # 確認產出的 MP3 不是空檔
                if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 100:
                    logging.info(f"[TTS_MS] ✓ {part_label}")
                    return True
                else:
                    raise ValueError("MP3 output is empty or too small")
            except Exception as e:
                # 清除殘留的損壞 MP3
                if os.path.exists(mp3_path):
                    try:
                        os.remove(mp3_path)
                    except Exception:
                        pass
                logging.warning(f"[TTS_MS] {part_label} attempt {attempt+1}/{max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2.0)  # 重試前稍等，避免過度轟炸服務

        logging.error(f"[TTS_MS] ✗ {part_label} 全部 {max_retries} 次嘗試均失敗")
        return False


async def _process_chapter_async(lines, book_title, chap_num, audio_dir, voice,
                                  ffmpeg_path, max_concurrency=5, max_retries=3):
    """
    並行非同步處理一章所有段落。
    - 最多同時 max_concurrency 個 Edge-TTS 並行請求。
    - 若任何段落最終失敗，回傳 ([], [])，整章廢棄。
    - 回傳 (generated_parts, valid_lines) 表示全部成功。
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    # 整理每個段落的中繼資訊
    task_metas = []
    for part_idx, line in enumerate(lines):
        text = line.strip()
        if not text:
            continue
        wav_name = f"{book_title}_chapter_{chap_num}_tmp_part_{part_idx+1:03d}.wav"
        mp3_name = f"{book_title}_chapter_{chap_num}_tmp_part_{part_idx+1:03d}.mp3"
        wav_path = os.path.join(audio_dir, wav_name)
        mp3_path = os.path.join(audio_dir, mp3_name)
        part_label = f"[Ch{chap_num} 段落 {part_idx+1}/{len([l for l in lines if l.strip()])}]"
        task_metas.append((text, mp3_path, wav_path, part_label))

    if not task_metas:
        logging.warning(f"[TTS_MS] 第 {chap_num} 章沒有有效文字段落")
        return [], []

    # 建立並行任務（已有 WAV 的直接跳過 TTS）
    coros = []
    skip_flags = []
    for text, mp3_path, wav_path, part_label in task_metas:
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
            logging.info(f"[TTS_MS] Resuming existing part: {os.path.basename(wav_path)}")
            coros.append(None)  # placeholder，表示不需要 TTS
            skip_flags.append(True)
        else:
            # 清除殘留的不完整 WAV
            if os.path.exists(wav_path):
                os.remove(wav_path)
            coros.append(_generate_one_segment(semaphore, text, mp3_path, part_label, voice, max_retries))
            skip_flags.append(False)

    # 並行執行所有 TTS 請求
    pending_coros = [(i, c) for i, (c, skip) in enumerate(zip(coros, skip_flags)) if not skip]
    if pending_coros:
        logging.info(f"[TTS_MS] 第 {chap_num} 章：並行生成 {len(pending_coros)} 段語音 (並行={max_concurrency})")
        results = await asyncio.gather(*[c for _, c in pending_coros])
        # 將結果對應回 coros 陣列
        result_map = {idx: res for (idx, _), res in zip(pending_coros, results)}
        # 檢查是否有任何段落失敗
        failed_count = sum(1 for res in results if not res)
        if failed_count > 0:
            logging.error(
                f"[TTS_MS] ✗ 第 {chap_num} 章有 {failed_count} 個段落 TTS 失敗，"
                f"放棄本章，避免拼接不完整音訊！"
            )
            # 清除所有已生成的暫存檔
            for _, mp3_path, wav_path, _ in task_metas:
                for p in [mp3_path, wav_path]:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
            return [], []
    else:
        result_map = {}

    # 所有 TTS 成功，將 MP3 轉換為 WAV
    generated_parts = []
    valid_lines = []
    for i, (text, mp3_path, wav_path, part_label) in enumerate(task_metas):
        if skip_flags[i]:
            # 已有完整 WAV，直接加入
            generated_parts.append(wav_path)
            valid_lines.append(text)
        else:
            # 需要將 MP3 轉成 WAV
            if not os.path.exists(mp3_path):
                logging.error(f"[TTS_MS] ✗ {part_label} MP3 不存在，整章放棄")
                # 清除已轉換的
                for p in generated_parts:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                return [], []
            try:
                subprocess.run(
                    [ffmpeg_path, "-y", "-i", mp3_path, wav_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                # 確認轉換後 WAV 不是空的
                if not os.path.exists(wav_path) or os.path.getsize(wav_path) < 100:
                    raise ValueError("轉換後的 WAV 是空檔")
                os.remove(mp3_path)
                generated_parts.append(wav_path)
                valid_lines.append(text)
            except Exception as e:
                logging.error(f"[TTS_MS] ✗ {part_label} MP3→WAV 轉換失敗: {e}，整章放棄")
                for p in [mp3_path] + generated_parts:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                return [], []

    return generated_parts, valid_lines


def run_tts_ms():
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
    voice = tts_cfg.get('edge_voice', tts_cfg.get('voice', 'zh-CN-YunxiNeural'))
    max_concurrency = int(tts_cfg.get('tts_concurrency', 5))
    max_retries = int(tts_cfg.get('tts_max_retries', 3))

    filenames = sorted([f for f in os.listdir(clean_text_dir) if f.endswith("_clean.txt")])

    succeeded_chapters = set()
    failed_chapters = set()

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

        # 最終輸出：{書名}_chapter_{N}.wav
        wav_filename = f"{book_title}_chapter_{chap_num}.wav"
        wav_path = os.path.join(audio_dir, wav_filename)

        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
            logging.info(f"[TTS_MS] Skipping existing: {wav_filename}")
            succeeded_chapters.add(int(chap_num))
            continue

        # ── 章節層級重試：最多嘗試 3 次（每次都是全章從頭重做）──
        CHAPTER_MAX_ATTEMPTS = 3
        chapter_success = False

        for chapter_attempt in range(1, CHAPTER_MAX_ATTEMPTS + 1):
            if chapter_attempt > 1:
                logging.warning(
                    f"[TTS_MS] ↻ 第 {chap_num} 章 第 {chapter_attempt}/{CHAPTER_MAX_ATTEMPTS} 次整章重試..."
                )
                # 清除上一次嘗試殘留的暫存 part 檔，確保從乾淨狀態開始
                import glob as _glob
                stale_parts = _glob.glob(os.path.join(
                    audio_dir, f"{book_title}_chapter_{chap_num}_tmp_part_*"
                ))
                for sp in stale_parts:
                    try:
                        os.remove(sp)
                    except Exception:
                        pass

            logging.info(
                f"[TTS_MS] ▶ 第 {chap_num} 章 嘗試 {chapter_attempt}/{CHAPTER_MAX_ATTEMPTS} "
                f"({len([l for l in lines if l.strip()])} 段，並行={max_concurrency})"
            )

            # ── Step 1: 非同步並行 TTS ──
            generated_parts, valid_lines = asyncio.run(
                _process_chapter_async(
                    lines, book_title, chap_num, audio_dir,
                    voice, ffmpeg_path, max_concurrency, max_retries
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
                if os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
                    srt_ok = True
                else:
                    logging.error(f"[TTS_MS] ✗ 第 {chap_num} 章嘗試 {chapter_attempt} SRT 輸出為空")
            except Exception as e:
                logging.error(f"[TTS_MS] ✗ 第 {chap_num} 章嘗試 {chapter_attempt} SRT 生成失敗: {e}")

            if not srt_ok:
                # 清除本次生成的 part WAV，下次重試從頭來
                for p in generated_parts:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                continue  # 進入下一次章節重試

            # ── Step 3: 合併 WAV ──
            merge_ok = False
            if len(generated_parts) == 1:
                shutil.move(generated_parts[0], wav_path)
                if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
                    logging.info(f"[TTS_MS] ✓ 第 {chap_num} 章 WAV 完成 (單段直接使用)")
                    merge_ok = True
            else:
                concat_list_path = wav_path + "_concat.txt"
                with open(concat_list_path, "w", encoding="utf-8") as f:
                    for p in generated_parts:
                        safe_path = p.replace("\\", "/")
                        f.write(f"file '{safe_path}'\n")
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                         "-i", concat_list_path, "-c", "copy", wav_path],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
                        logging.info(
                            f"[TTS_MS] ✓ 第 {chap_num} 章 WAV 合併完成 ({len(generated_parts)} 段)"
                        )
                        merge_ok = True
                        for p in generated_parts:
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                    else:
                        raise ValueError("合併後 WAV 是空檔")
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
                # 清除殘留，等待下次重試
                for p in generated_parts:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
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
            failed_chapters.add(int(chap_num))

    return succeeded_chapters, failed_chapters


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    succeeded, failed = run_tts_ms()
    if failed:
        logging.error(f"[TTS_MS] === 最終失敗章節 (共 {len(failed)} 章): {sorted(failed)} ===")
    else:
        logging.info(f"[TTS_MS] === 全部 {len(succeeded)} 章 TTS 成功 ===")
