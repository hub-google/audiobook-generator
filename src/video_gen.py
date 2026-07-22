import os
import glob
import re
import yaml
import subprocess
import wave
import contextlib
import logging

import shutil
import time

def get_ffmpeg_path():
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    local_path = r"C:\Users\cyt18\anaconda3\Library\bin\ffmpeg.exe"
    if os.path.exists(local_path):
        return local_path
    return "ffmpeg"

FFMPEG_PATH = get_ffmpeg_path()

# 字型路徑（支援 Windows 與 Linux / GitHub Actions）
FONT_PATHS = [
    # Linux (Ubuntu / GitHub Actions)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",      # Microsoft YaHei
    r"C:\Windows\Fonts\msjh.ttc",      # Microsoft JhengHei（繁體）
    r"C:\Windows\Fonts\simsun.ttc",    # SimSun fallback
]


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_wav_duration(wav_path):
    with contextlib.closing(wave.open(wav_path, 'r')) as f:
        frames = f.getnframes()
        rate = f.getframerate()
        return frames / float(rate)


def format_timestamp(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    else:
        return f"{m:02d}:{s:02d}"


def parse_chapter_num(wav_filename):
    """從檔名解析章節號碼，回傳整數（方便排序）。"""
    m = re.search(r'chapter_(\d+)', wav_filename)
    if m:
        return int(m.group(1))
    return 9999


def get_chapter_title(workspace_dir, book_title, chap_num):
    """
    嘗試從 RawText 讀取章節標題（第一行）。
    找不到時回傳預設 '第N章'。
    """
    raw_path = os.path.join(workspace_dir, "RawText", f"{book_title}_chapter_{chap_num}_raw.txt")
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            if first_line:
                return first_line
        except Exception:
            pass
    return f"第{chap_num}章"


def get_font(size):
    """取得可用的中文字型，回傳 PIL ImageFont 物件。"""
    try:
        from PIL import ImageFont
        for font_path in FONT_PATHS:
            if os.path.exists(font_path):
                return ImageFont.truetype(font_path, size)
        # fallback：PIL 預設字型（不支援中文，但不至於報錯）
        return ImageFont.load_default()
    except Exception:
        return None


def generate_chapter_title_image(book_title, chap_num, chapter_title, output_path, workspace_dir=""):
    """
    呼叫 image_gen 生成含 50 字 AI 劇情摘要與避開 CC 字幕區域的標題卡圖片
    """
    from image_gen import generate_title_card
    from summary_gen import get_or_generate_chapter_summary
    
    summary_text = ""
    if workspace_dir:
        summary_text = get_or_generate_chapter_summary(workspace_dir, book_title, chap_num)
        
    return generate_title_card(book_title, chap_num, chapter_title, output_path, summary_text=summary_text)


def generate_chapter_video(book_title, wav_path, workspace_dir, output_dir, fallback_images):
    """
    為單一 chapter WAV 生成對應的 MP4。
    使用章節標題卡圖片，編碼速度極快（無 zoompan）。
    已存在則跳過。
    """
    wav_name = os.path.basename(wav_path)
    chap_num = parse_chapter_num(wav_name)
    output_video = os.path.join(output_dir, f"{book_title}_chapter_{chap_num}.mp4")

    if os.path.exists(output_video):
        logging.info(f"[VideoGen] Skipping existing: {os.path.basename(output_video)}")
        return output_video, get_wav_duration(wav_path)

    duration = get_wav_duration(wav_path)
    logging.info(f"[VideoGen] Generating chapter {chap_num} video (audio: {duration:.1f}s) ...")

    # ── 取得章節標題並產生標題卡 (包含 50 字 AI 劇情摘要) ──
    chapter_title = get_chapter_title(workspace_dir, book_title, chap_num)
    title_card_path = os.path.join(workspace_dir, "Images", f"{book_title}_chapter_{chap_num}.jpg")
    os.makedirs(os.path.dirname(title_card_path), exist_ok=True)

    # 強制產生最新包含 AI 摘要的標題卡圖片
    card_ok = generate_chapter_title_image(book_title, chap_num, chapter_title, title_card_path, workspace_dir=workspace_dir)

    # 若 Pillow 產圖失敗，fallback 到原有背景圖
    if card_ok and os.path.exists(title_card_path):
        img_to_use = title_card_path
    elif fallback_images:
        img_to_use = fallback_images[(chap_num - 1) % len(fallback_images)]
        logging.warning(f"[VideoGen] Using fallback image for chapter {chap_num}")
    else:
        logging.error(f"[VideoGen] No image available for chapter {chap_num}!")
        return None, 0

    # ── 檢查是否有對應 SRT 字幕檔並進行內嵌硬字幕（Hardsub）──
    srt_path = os.path.join(workspace_dir, "Subtitles", f"{book_title}_chapter_{chap_num}.srt")
    vf_filter = (
        "scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
        "format=yuv420p"
    )
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 10:
        escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
        vf_filter = (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
            f"subtitles='{escaped_srt}':force_style='FontSize=22,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=25',"
            "format=yuv420p"
        )
        logging.info(f"[VideoGen] 💬 已開啟 FFmpeg 硬字幕嵌入: {os.path.basename(srt_path)}")

    # ── FFmpeg：靜態圖 + 音訊 + 硬字幕 → MP4 ──
    cmd = [
        FFMPEG_PATH, "-y",
        "-loop", "1",
        "-framerate", "1",
        "-i", img_to_use,
        "-i", wav_path,
        "-vf", vf_filter,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-r", "1",
        "-threads", "0",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_video
    ]
    t0 = time.time()
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elapsed = time.time() - t0
    logging.info(f"[VideoGen] 🎉 Chapter {chap_num} MP4 generated successfully -> {os.path.basename(output_video)} (took {elapsed:.1f}s)")
    return output_video, duration


def run_video_gen():
    config = load_config()
    book_title = config['book_title']

    workspace_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", config['paths']['workspace_base'], book_title
    ))
    audio_dir   = os.path.join(workspace_dir, "Audio")
    images_dir  = os.path.join(workspace_dir, "Images")

    # 各章節 MP4 放在 Workspace 的 Video 子目錄（中間暫存）
    video_dir = os.path.join(workspace_dir, "Video")
    if not os.path.exists(video_dir):
        os.makedirs(video_dir)

    # 最終成品放在 Output（full.mp4 + metadata）
    output_dir = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "Output", book_title
    ))
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 找所有 chapter WAV（排除 _tmp_part_ 暫存檔）
    all_wavs = sorted(
        glob.glob(os.path.join(audio_dir, "*.wav")),
        key=lambda p: parse_chapter_num(os.path.basename(p))
    )
    wav_files = [w for w in all_wavs if "_tmp_part_" not in os.path.basename(w)]

    if not wav_files:
        logging.warning("[VideoGen] No wav files found in Audio directory.")
        return

    # fallback 背景圖（若 Pillow 失敗時使用）
    fallback_images = sorted(
        glob.glob(os.path.join(images_dir, "*.png")) +
        glob.glob(os.path.join(images_dir, "*.jpg"))
    )

    logging.info(f"[VideoGen] Found {len(wav_files)} chapter wav(s). Generating per-chapter MP4s ...")

    chapter_mp4s      = []
    chapter_durations = {}
    chapter_srt_paths = []
    chapter_duration_list = []
    total_duration    = 0.0

    for wav_path in wav_files:
        mp4_path, dur = generate_chapter_video(
            book_title, wav_path, workspace_dir, video_dir, fallback_images
        )
        if mp4_path:
            chapter_mp4s.append(mp4_path)
        chap_num = parse_chapter_num(os.path.basename(wav_path))
        chapter_title = get_chapter_title(workspace_dir, book_title, chap_num)
        chapter_durations[chapter_title] = dur
        chapter_duration_list.append(dur)
        srt_path = os.path.join(workspace_dir, "Subtitles", f"{book_title}_chapter_{chap_num}.srt")
        chapter_srt_paths.append(srt_path)
        total_duration += dur

    logging.info(f"[VideoGen] Total audio duration: {total_duration:.2f}s ({total_duration/3600:.2f}h)")

    # ── 產生 YouTube Metadata（章節時間戳）──
    metadata_path = os.path.join(output_dir, "youtube_metadata.txt")
    with open(metadata_path, "w", encoding="utf-8") as f:
        f.write(f"【{book_title}】沉浸式有聲書 | AI語音合成\n\n")
        f.write("⏳ 章節時間戳：\n")
        current_time = 0.0
        for chap_title, dur in chapter_durations.items():
            f.write(f"{format_timestamp(current_time)} {chap_title}\n")
            current_time += dur
        f.write("\n\n---\n")
        f.write("⚠️ 本內容採用 AI 輔助製作，配音與視覺皆經二次原創優化處理。\n")
    logging.info(f"[VideoGen] YouTube Metadata -> {metadata_path}")

    # ── 合併 SRT 字幕檔 ──
    try:
        from subtitle_gen import merge_srts
        full_srt_path = os.path.join(output_dir, f"{book_title}_full.srt")
        merge_srts(chapter_srt_paths, chapter_duration_list, full_srt_path)
    except Exception as e:
        logging.error(f"[VideoGen] ✗ Full SRT merge failed: {e}")

    # ── 自動進行 10~11 小時無縫分部 (Part) 影片與 Metadata 切分打包 ──
    try:
        from part_builder import build_all_parts
        logging.info("[VideoGen] 正在執行 10~11 小時影片自動無縫分部 (Part) 打包...")
        built_parts = build_all_parts(book_title, workspace_dir=workspace_dir, output_dir=output_dir, min_hours=10.0, max_hours=11.0)
        logging.info(f"[VideoGen] 🎉 成功生成 {len(built_parts)} 部 10~11 小時分部影片！")
    except Exception as e:
        logging.error(f"[VideoGen] ✗ 分部打包失敗: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_video_gen()
