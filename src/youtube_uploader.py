"""
youtube_uploader.py — 單獨獨立的 YouTube RTMP 直播 / 影片推流工具

用法 (Usage):
  1. 單一影片推流：
     python src/youtube_uploader.py --input Output/MyBook/MyBook_full.mp4 --stream-key "2sep-1xus-zvp0-gqdb-43fr"

  2. 資料夾多部影片按順序推流：
     python src/youtube_uploader.py --input Workspace/MyBook/Video/ --stream-key "2sep-1xus-zvp0-gqdb-43fr"

  3. 使用環境變數帶入串流金鑰：
     $env:YOUTUBE_STREAM_KEY="2sep-1xus-zvp0-gqdb-43fr"
     python src/youtube_uploader.py --input Workspace/MyBook/Video/
"""

import os
import sys
import glob
import re
import subprocess
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [YouTube-Uploader] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)

def parse_chapter_number(filepath):
    m = re.search(r'chapter_(\d+)', filepath)
    if m:
        return int(m.group(1))
    return 999999

def stream_single_file(video_path, rtmp_url):
    filename = os.path.basename(video_path)
    logging.info(f"▶️ 正在傳送影片至 YouTube Live: {filename}")
    
    cmd = [
        "ffmpeg",
        "-re",
        "-i", video_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-f", "flv",
        rtmp_url
    ]
    
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode != 0:
        logging.error(f"❌ 傳送失敗 [{filename}]: {process.stderr[-500:]}")
        return False
    
    logging.info(f"✅ 成功傳送: {filename}")
    return True

def main():
    parser = argparse.ArgumentParser(description="Standalone YouTube RTMP Streamer / Uploader")
    parser.add_argument("-i", "--input", required=True, help="Path to single MP4 file or directory containing MP4 files")
    parser.add_argument("-k", "--stream-key", default="", help="YouTube Stream Key (or set YOUTUBE_STREAM_KEY env var)")
    args = parser.parse_args()

    stream_key = args.stream_key or os.environ.get("YOUTUBE_STREAM_KEY")
    if not stream_key:
        logging.error("❌ 錯誤：未提供 YouTube 串流金鑰 (Stream Key)！請透過 --stream-key 或設定 YOUTUBE_STREAM_KEY 環境變數。")
        sys.exit(1)

    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    input_path = os.path.abspath(args.input)

    if not os.path.exists(input_path):
        logging.error(f"❌ 錯誤：找不到指定的輸入路徑: {input_path}")
        sys.exit(1)

    # 判斷是單一檔案還是資料夾
    if os.path.isfile(input_path):
        video_files = [input_path]
    else:
        video_files = glob.glob(os.path.join(input_path, "**", "*.mp4"), recursive=True)
        video_files.sort(key=parse_chapter_number)

    if not video_files:
        logging.error(f"❌ 在 {input_path} 中找不到任何 .mp4 檔案！")
        sys.exit(1)

    logging.info(f"🚀 開始執行 YouTube 上傳/推流任務 (共 {len(video_files)} 個 MP4 檔案)")

    success_count = 0
    for idx, video_file in enumerate(video_files, 1):
        logging.info(f"\n[{idx}/{len(video_files)}] 準備處理: {video_file}")
        if stream_single_file(video_file, rtmp_url):
            success_count += 1

    logging.info(f"\n🎉 任務完成！共成功上傳/推流 {success_count}/{len(video_files)} 部影片至 YouTube。")

if __name__ == "__main__":
    main()
