import os
import yaml
import logging
import subprocess
import asyncio
import edge_tts

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

async def generate_chapter_audio(text, output_file, voice="zh-CN-YunxiNeural"):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_file)

import shutil

def get_ffmpeg_path():
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    local_path = r"C:\Users\cyt18\anaconda3\Library\bin\ffmpeg.exe"
    if os.path.exists(local_path):
        return local_path
    return "ffmpeg"

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

        logging.info(f"[TTS_MS] Processing {filename} ({len(lines)} segments)...")
        
        generated_parts = []
        valid_lines = []

        for part_idx, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue

            audio_filename = f"{book_title}_chapter_{chap_num}_tmp_part_{part_idx+1:03d}.wav"
            audio_path = os.path.join(audio_dir, audio_filename)
            mp3_path = audio_path.replace('.wav', '.mp3')

            if os.path.exists(audio_path):
                logging.info(f"[TTS_MS] Resuming existing part: {audio_filename}")
                generated_parts.append(audio_path)
                valid_lines.append(text)
                continue

            try:
                logging.info(f"[TTS_MS] Generating [{part_idx+1}/{len(lines)}] {audio_filename} ...")
                # 產生 MP3
                asyncio.run(generate_chapter_audio(text, mp3_path))
                
                # 使用 ffmpeg 轉成 WAV
                subprocess.run(
                    [ffmpeg_path, "-y", "-i", mp3_path, audio_path],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                
                # 刪除暫存 MP3
                if os.path.exists(mp3_path):
                    os.remove(mp3_path)
                    
                generated_parts.append(audio_path)
                valid_lines.append(text)
            except Exception as e:
                logging.error(f"[TTS_MS] ✗ Failed to generate {audio_filename}: {e}")
                if os.path.exists(mp3_path):
                    try:
                        os.remove(mp3_path)
                    except Exception:
                        pass
        
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
                import shutil
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
