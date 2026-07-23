"""
test_merge_all.py — 將 Workspace/ 下所有 worker 產出的章節 MP4 無損合併為單一整本 MP4 影片，並統計檔案大小與時長。
"""

import os
import sys
import glob
import re
import shutil
import subprocess
import logging

SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.insert(0, SRC_DIR)

from part_builder import FFMPEG_PATH, FFPROBE_PATH, parse_chapter_num, get_media_duration, format_timestamp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Merge-Test] %(levelname)s %(message)s"
)

def merge_all_mp4s(workspace_dir="Workspace", output_dir="Output"):
    logging.info(f"🔍 正在搜尋 {workspace_dir} 目錄下的所有章節 MP4 影片...")

    all_mp4s = glob.glob(os.path.join(workspace_dir, "**", "*.mp4"), recursive=True)
    chapter_files = []
    
    for f in all_mp4s:
        base_name = os.path.basename(f)
        # 排除包含 Part_ 或 Full_Merged 的舊合併檔
        if "Part_" in base_name or "Full_Merged" in base_name or "merged" in base_name.lower():
            continue
        c_num = parse_chapter_num(base_name)
        if c_num != 999999:
            chapter_files.append((c_num, os.path.abspath(f)))

    if not chapter_files:
        logging.error(f"❌ 在 {workspace_dir} 內找不到任何有效的章節 MP4 檔案！")
        return False

    # 按章節編號升序排序
    chapter_files.sort(key=lambda x: x[0])
    
    # 避免重複章節
    unique_chapters = {}
    for c_num, fpath in chapter_files:
        if c_num not in unique_chapters:
            unique_chapters[c_num] = fpath
        else:
            logging.warning(f"⚠️ 發現重複章節 Ch {c_num}: 優先選用 {fpath}")

    sorted_files = [unique_chapters[k] for k in sorted(unique_chapters.keys())]
    min_chap = sorted(unique_chapters.keys())[0]
    max_chap = sorted(unique_chapters.keys())[-1]

    logging.info(f"✅ 共找到 {len(sorted_files)} 個獨立章節 MP4 (章節涵蓋: 第 {min_chap} 章 ~ 第 {max_chap} 章)")

    os.makedirs(output_dir, exist_ok=True)
    output_filename = f"Full_Book_Merged_Ch{min_chap:04d}_to_Ch{max_chap:04d}.mp4"
    output_path = os.path.join(output_dir, output_filename)
    concat_list_path = os.path.join(output_dir, "concat_all_list.txt")

    with open(concat_list_path, "w", encoding="utf-8") as f:
        for mp4_path in sorted_files:
            safe_p = mp4_path.replace("\\", "/")
            f.write(f"file '{safe_p}'\n")

    logging.info(f"🎬 開始使用 FFmpeg Concat Demuxer (-c copy) 進行全書無損合併...")
    cmd = [
        FFMPEG_PATH, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        output_path
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if os.path.exists(concat_list_path):
        try:
            os.remove(concat_list_path)
        except Exception:
            pass

    if res.returncode == 0 and os.path.exists(output_path):
        size_bytes = os.path.getsize(output_path)
        size_mb = size_bytes / (1024 * 1024)
        size_gb = size_bytes / (1024 * 1024 * 1024)
        
        total_duration_sec = get_media_duration(output_path)
        formatted_time = format_timestamp(total_duration_sec)
        hours = total_duration_sec / 3600.0

        logging.info("=" * 60)
        logging.info("🎉🎉🎉 【全書單一 MP4 合併成功】 🎉🎉🎉")
        logging.info(f"  • 合併檔名: {output_filename}")
        logging.info(f"  • 包含章節: 第 {min_chap} 章 ~ 第 {max_chap} 章 (共 {len(sorted_files)} 章)")
        logging.info(f"  • 影片總長: {formatted_time} ({hours:.2f} 小時)")
        logging.info(f"  • 檔案大小: {size_mb:.2f} MB ({size_gb:.3f} GB)")
        logging.info(f"  • YouTube 上限對比 (檔案): {size_gb:.3f} GB / 256.00 GB (佔比 {size_gb/256*100:.2f}%)")
        logging.info(f"  • YouTube 上限對比 (時長): {hours:.2f} 小時 / 12.00 小時 (佔比 {hours/12*100:.2f}%)")
        logging.info("=" * 60)
        return True
    else:
        logging.error(f"❌ 全書影片合併失敗: {res.stderr}")
        return False

if __name__ == "__main__":
    ws_dir = sys.argv[1] if len(sys.argv) > 1 else "Workspace"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "Output"
    merge_all_mp4s(ws_dir, out_dir)
