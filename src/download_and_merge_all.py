"""
download_and_merge_all.py — 本地一鍵下載 GitHub Artifacts 並自動合成所有章節 MP4
"""
import os
import sys
import glob
import json
import shutil
import subprocess
import logging

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Downloader] %(levelname)s %(message)s"
)

def download_run_artifacts(run_id, repo="hub-google/audiobook-generator", dest_dir="Downloaded_MP4s"):
    os.makedirs(dest_dir, exist_ok=True)
    logging.info(f"📥 正在從 GitHub 下載 Run #{run_id} 的所有影片 Artifacts 至 {dest_dir}...")
    
    cmd = [
        "gh", "run", "download", str(run_id),
        "--repo", repo,
        "--dir", dest_dir
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode == 0:
        logging.info("✅ Artifacts 下載完成！")
        return True
    else:
        logging.error(f"❌ 下載失敗: {res.stderr}")
        return False

def merge_downloaded_mp4s(book_title="凡人修仙傳", dest_dir="Downloaded_MP4s", output_dir="Output"):
    from part_builder import partition_chapters, merge_part_videos, parse_chapter_num
    
    mp4_files = sorted(glob.glob(os.path.join(dest_dir, "**", "*.mp4"), recursive=True), key=lambda p: parse_chapter_num(os.path.basename(p)))
    if not mp4_files:
        logging.warning(f"⚠️ 在 {dest_dir} 目錄下未找到任何 MP4 檔案！")
        return
    
    logging.info(f"找到 {len(mp4_files)} 個單章 MP4 檔案，開始自動切分與合成 Part 影片...")
    parts = partition_chapters(mp4_files, min_hours=10.0, max_hours=11.0)
    
    parts_out_dir = os.path.join(output_dir, book_title, "Parts")
    os.makedirs(parts_out_dir, exist_ok=True)
    
    for p in parts:
        part_num = p["part_num"]
        s_c = p["start_chap"]
        e_c = p["end_chap"]
        out_name = f"{book_title}_Part_{part_num:02d}_Ch{s_c:04d}_to_Ch{e_c:04d}.mp4"
        out_path = os.path.join(parts_out_dir, out_name)
        
        logging.info(f"🚀 合成第 {part_num} 部 ({s_c}~{e_c} 章) -> {out_name}")
        merge_part_videos(p, out_path)

if __name__ == "__main__":
    run_id = sys.argv[1] if len(sys.argv) > 1 else "29919238796"
    download_run_artifacts(run_id)
    merge_downloaded_mp4s()
