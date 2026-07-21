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
            text = f.read().strip()
        
        if not text:
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

        # 暫存 MP3（edge_tts 直接輸出 mp3）
        mp3_filename = f"{book_title}_chapter_{chap_num}_tmp.mp3"
        mp3_path = os.path.join(audio_dir, mp3_filename)

        logging.info(f"[TTS_MS] Generating MS TTS for {filename} -> {wav_filename} ...")
        
        try:
            # 產生 MP3
            asyncio.run(generate_chapter_audio(text, mp3_path))
            
            # 使用 ffmpeg 轉成 WAV
            logging.info(f"[TTS_MS] Converting MP3 to WAV...")
            subprocess.run(
                [ffmpeg_path, "-y", "-i", mp3_path, wav_path],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            
            # 刪除暫存 MP3
            os.remove(mp3_path)
            
            logging.info(f"[TTS_MS] ✓ Saved: {wav_filename}")
        except Exception as e:
            logging.error(f"[TTS_MS] ✗ Failed to generate {wav_filename}: {e}")
            # 清除殘留暫存檔
            if os.path.exists(mp3_path):
                try:
                    os.remove(mp3_path)
                except Exception:
                    pass

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_tts_ms()
