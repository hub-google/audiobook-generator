import os
import time
import requests
import yaml
import logging
import subprocess

# GPT-SoVITS 環境的 Python（透過 Junction C:\GPT-SoVITS 對應桌面\GPT-SoVITS）
GPTSOVITS_PYTHON = r"C:\Users\cyt18\anaconda3\envs\GPTSoVits\python.exe"
# Junction 路徑（純英文），讓 Python 環境的 .pth 能正確載入
GPTSOVITS_CWD = r"C:\GPT-SoVITS"

FFMPEG_PATH = r"C:\Users\cyt18\anaconda3\Library\bin\ffmpeg.exe"

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def start_gpt_sovits_api(api_url, max_wait=90):
    """
    啟動 GPT-SoVITS API 伺服器（在新視窗中跑），然後等待它就緒。
    使用直接路徑呼叫 GPTSoVits 環境的 python.exe，不需要 conda activate。
    """
    logging.info("[Init] Checking if GPT-SoVITS API is already running...")
    try:
        requests.get(api_url, timeout=2)
        logging.info("[Init] GPT-SoVITS API is already running!")
        return None  # 已在跑，不需要再啟動
    except Exception:
        pass

    logging.info("[Init] Starting GPT-SoVITS API Server in a new window...")
    logging.info(f"[Init]   Python: {GPTSOVITS_PYTHON}")
    logging.info(f"[Init]   CWD:    {GPTSOVITS_CWD}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    
    process = subprocess.Popen(
        [GPTSOVITS_PYTHON, "api_v2.py"],
        cwd=GPTSOVITS_CWD,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    logging.info(f"[Init] API process started (PID: {process.pid}). Waiting for it to be ready (max {max_wait}s)...")
    start = time.time()
    dot_count = 0
    while time.time() - start < max_wait:
        try:
            requests.get(api_url, timeout=2)
            logging.info(f"\n[Init] ✓ GPT-SoVITS API is ready! (took {time.time()-start:.0f}s)")
            return process
        except Exception:
            elapsed = int(time.time() - start)
            dot_count += 1
            if dot_count % 5 == 0:
                logging.info(f"[Init] Still waiting... ({elapsed}s elapsed)")
            time.sleep(1)

    logging.warning(f"[Init] ⚠ Timeout after {max_wait}s. API might not be ready. TTS will fail if API is not up.")
    return process


def merge_parts_to_chapter(part_paths, chapter_wav_path):
    """
    用 ffmpeg concat demuxer 把多個 part WAV 無損合併成單一 chapter WAV。
    合併完後刪除 part 檔。
    """
    if not part_paths:
        return False

    if len(part_paths) == 1:
        # 只有一個 part，直接重命名
        os.rename(part_paths[0], chapter_wav_path)
        logging.info(f"[TTS] ✓ Renamed single part to: {os.path.basename(chapter_wav_path)}")
        return True

    # 建立 concat 清單（臨時檔）
    concat_list_path = chapter_wav_path + "_concat.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in part_paths:
            # ffmpeg concat demuxer 需要用正斜線或跳脫
            safe_path = p.replace("\\", "/")
            f.write(f"file '{safe_path}'\n")

    try:
        subprocess.run(
            [FFMPEG_PATH, "-y",
             "-f", "concat", "-safe", "0",
             "-i", concat_list_path,
             "-c", "copy",          # 無損複製，速度極快
             chapter_wav_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logging.info(f"[TTS] ✓ Merged {len(part_paths)} parts -> {os.path.basename(chapter_wav_path)}")

        # 刪除 part 檔
        for p in part_paths:
            try:
                os.remove(p)
            except Exception:
                pass
        return True
    except Exception as e:
        logging.error(f"[TTS] ✗ Merge failed for {os.path.basename(chapter_wav_path)}: {e}")
        return False
    finally:
        if os.path.exists(concat_list_path):
            try:
                os.remove(concat_list_path)
            except Exception:
                pass


def run_tts():
    config = load_config()
    book_title = config['book_title']
    tts_config = config['tts']

    workspace_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", config['paths']['workspace_base'], book_title)
    clean_text_dir = os.path.join(workspace_dir, "CleanText")
    audio_dir = os.path.join(workspace_dir, "Audio")

    if not os.path.exists(audio_dir):
        os.makedirs(audio_dir)

    api_url = tts_config.get('api_url', 'http://127.0.0.1:9880')
    start_gpt_sovits_api(api_url)

    if not os.path.exists(clean_text_dir):
        logging.error(f"[TTS] CleanText directory not found: {clean_text_dir}")
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

        # 最終輸出的章節 WAV（無 part）
        chapter_wav = f"{book_title}_chapter_{chap_num}.wav"
        chapter_wav_path = os.path.join(audio_dir, chapter_wav)

        if os.path.exists(chapter_wav_path):
            logging.info(f"[TTS] Skipping existing chapter: {chapter_wav}")
            continue

        logging.info(f"[TTS] Processing {filename} ({len(lines)} segments)...")
        tts_url = f"{api_url}/tts"
        ref_audio_original = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", tts_config['ref_audio_path']))
        
        import shutil
        import tempfile
        ref_audio = os.path.join(tempfile.gettempdir(), "safe_ref_audio.wav")
        shutil.copy2(ref_audio_original, ref_audio)

        generated_parts = []

        for part_idx, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue

            # part 檔命名加上 _tmp_ 前綴，方便辨識是暫存
            audio_filename = f"{book_title}_chapter_{chap_num}_tmp_part_{part_idx+1:03d}.wav"
            audio_path = os.path.join(audio_dir, audio_filename)

            if os.path.exists(audio_path):
                logging.info(f"[TTS] Resuming existing part: {audio_filename}")
                generated_parts.append(audio_path)
                continue

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logging.info(f"[TTS] Generating [{part_idx+1}/{len(lines)}] {audio_filename} ...")
                    response = requests.get(tts_url, params={
                        "text": text,
                        "text_lang": tts_config['text_lang'],
                        "ref_audio_path": ref_audio,
                        "prompt_text": tts_config['prompt_text'],
                        "prompt_lang": tts_config['prompt_lang']
                    }, timeout=300)
                    response.raise_for_status()
                    with open(audio_path, "wb") as f:
                        f.write(response.content)
                    logging.info(f"[TTS] ✓ Saved part: {audio_filename}")
                    generated_parts.append(audio_path)
                    break
                except Exception as e:
                    err_msg = ""
                    if 'response' in locals() and hasattr(response, 'text'):
                        err_msg = f" API Response: {response.text}"
                    logging.error(f"[TTS] Attempt {attempt+1}/{max_retries} failed for {audio_filename}: {e}{err_msg}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                    else:
                        logging.error(f"[TTS] ✗ Max retries reached. Skipping {audio_filename}.")

        # 所有 part 產生完後，合併成單一 chapter WAV
        if generated_parts:
            merge_parts_to_chapter(generated_parts, chapter_wav_path)
        else:
            logging.warning(f"[TTS] No parts generated for chapter {chap_num}, skipping merge.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_tts()
