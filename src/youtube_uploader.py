"""
youtube_uploader.py — 單獨獨立的 YouTube RTMP 直播 / 影片推流工具 (支援多 Stream Key 自動避開忙碌 Key)

用法 (Usage):
  1. 單一影片推流：
     python src/youtube_uploader.py --input Output/MyBook/MyBook_full.mp4 --stream-key "2sep-1xus-zvp0-gqdb-43fr"

  2. 資料夾多部影片按順序推流：
     python src/youtube_uploader.py --input Workspace/MyBook/Video/ --stream-key "2sep-1xus-zvp0-gqdb-43fr"

  3. 使用環境變數自動尋找空閒 Stream Key：
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

def get_candidate_stream_keys(cli_key=""):
    candidates = []
    if cli_key:
        candidates.append(cli_key.strip())

    for env in ["YOUTUBE_STREAM_KEY", "YOUTUBE_STREAM_KEY_2", "YOUTUBE_STREAM_KEY_3", "YOUTUBE_STREAM_KEY_4", "YOUTUBE_STREAM_KEY_5"]:
        val = os.environ.get(env, "").strip()
        if val and val not in candidates:
            candidates.append(val)
            
    keys_str = os.environ.get("YOUTUBE_STREAM_KEYS", "").strip()
    if keys_str:
        for k in keys_str.split(","):
            k = k.strip()
            if k and k not in candidates:
                candidates.append(k)
    return candidates

def test_and_select_stream_key(candidate_keys):
    if not candidate_keys:
        return None

    if len(candidate_keys) == 1:
        return candidate_keys[0]

    logging.info(f"🔍 檢測到共有 {len(candidate_keys)} 組 Stream Key 備選池，開始自動偵測空閒 Key...")

    for idx, key in enumerate(candidate_keys, 1):
        key_masked = f"{key[:4]}****{key[-4:]}" if len(key) >= 8 else "****"
        logging.info(f"   • [{idx}/{len(candidate_keys)}] 檢測 Key ({key_masked})...")
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{key}"
        
        cmd = [
            "ffmpeg", "-y",
            "-re",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=15",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-f", "flv",
            rtmp_url
        ]
        
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
            if res.returncode == 0:
                logging.info(f"✅ Key [{key_masked}] 狀態【空閒可用】！選定此 Key 進行推流。")
                return key
            else:
                logging.warning(f"⚠️ Key [{key_masked}] 【已被佔用中】，自動切換下一組...")
        except Exception as e:
            logging.warning(f"⚠️ Key [{key_masked}] 測試異常: {e}，切換下一組...")

    logging.warning("⚠️ 備選池中所有 Key 均回應忙碌，預設使用第一組 Key 執行。")
    return candidate_keys[0]

def main():
    parser = argparse.ArgumentParser(description="Standalone YouTube RTMP Streamer / Uploader with Multi-Key Support")
    parser.add_argument("-i", "--input", required=True, help="Path to single MP4 file or directory containing MP4 files")
    parser.add_argument("-k", "--stream-key", default="", help="YouTube Stream Key (or set YOUTUBE_STREAM_KEY env var)")
    args = parser.parse_args()

    candidate_keys = get_candidate_stream_keys(args.stream_key)
    if not candidate_keys:
        logging.error("❌ 錯誤：未提供任何 YouTube 串流金鑰 (Stream Key)！")
        sys.exit(1)

    selected_key = test_and_select_stream_key(candidate_keys)
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{selected_key}"
    input_path = os.path.abspath(args.input)

    if not os.path.exists(input_path):
        logging.error(f"❌ 錯誤：找不到指定的輸入路徑: {input_path}")
        sys.exit(1)

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
