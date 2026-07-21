import os
import sys
import glob
import re
import shutil
import subprocess
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

def parse_chapter_number(filepath):
    m = re.search(r'chapter_(\d+)', filepath)
    if m:
        return int(m.group(1))
    return 999999

def stream_file_to_rtmp(mp4_path, rtmp_url):
    filename = os.path.basename(mp4_path)
    logging.info(f"▶️ 全速推流至 YouTube Live: {filename}")
    # 移除 -re 參數，讓 FFmpeg 善用 GitHub 雲端大網速全速推流，幾十分鐘即可推完上百小時的內容！
    cmd = [
        "ffmpeg",
        "-i", mp4_path,
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
    logging.info(f"✅ 完成推流: {filename}")
    return True

def get_run_artifact_names(run_id, repo):
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts", "--jq", ".artifacts[].name"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"Failed to fetch artifacts for run {run_id}: {res.stderr}")
        return []
    names = [n.strip() for n in res.stdout.splitlines() if n.strip().startswith("video-worker-")]
    names.sort(key=lambda x: int(re.search(r'\d+', x).group()) if re.search(r'\d+', x) else 999)
    return names

def get_candidate_stream_keys():
    """收集環境變數中設定的所有 Stream Keys (KEY 1, KEY 2, KEY 3 ...)"""
    candidates = []
    env_vars = ["YOUTUBE_STREAM_KEY", "YOUTUBE_STREAM_KEY_2", "YOUTUBE_STREAM_KEY_3", "YOUTUBE_STREAM_KEY_4", "YOUTUBE_STREAM_KEY_5"]
    for env in env_vars:
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
    """
    廣播池檢測：依序測試 Candidate Stream Keys，若某個 Key 目前已被其他任務直播佔用，
    則自動跳過並切換至下一組空閒 Key，防止踢下線目前正在進行的直播。
    """
    if not candidate_keys:
        return None

    if len(candidate_keys) == 1:
        return candidate_keys[0]

    logging.info(f"🔍 檢測到共有 {len(candidate_keys)} 組 Stream Key 備選池，開始自動偵測空閒 Key...")

    for idx, key in enumerate(candidate_keys, 1):
        key_masked = f"{key[:4]}****{key[-4:]}" if len(key) >= 8 else "****"
        logging.info(f"   • [{idx}/{len(candidate_keys)}] 檢測 Key ({key_masked})...")
        rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{key}"
        
        # 用 2 秒無聲黑底輕量測試 Probe
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
                logging.info(f"✅ Key [{key_masked}] 檢測結果：狀態【空閒可用】！選定此 Key 進行推流。")
                return key
            else:
                logging.warning(f"⚠️ Key [{key_masked}] 檢測結果：【正在使用中/已被佔用】，自動跳過切換下一組...")
        except Exception as e:
            logging.warning(f"⚠️ Key [{key_masked}] 測試超時或連線異常: {e}，切換下一組...")

    logging.warning("⚠️ 備選池中所有 Key 均回應忙碌，預設使用第一組 Key 執行。")
    return candidate_keys[0]

def main():
    parser = argparse.ArgumentParser(description="Accelerated YouTube Live Streamer with Multi-Key Failover")
    parser.add_argument("--run-id", required=True, help="GitHub Actions Run ID (e.g. 29821206020)")
    parser.add_argument("--repo", default="hub-google/audiobook-generator", help="GitHub Repository")
    args = parser.parse_args()

    candidate_keys = get_candidate_stream_keys()
    if not candidate_keys:
        logging.error("CRITICAL: No YOUTUBE_STREAM_KEY environment variables found!")
        sys.exit(1)

    selected_key = test_and_select_stream_key(candidate_keys)
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{selected_key}"

    # 動態引入 metadata_gen
    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, SRC_DIR)
    from metadata_gen import save_book_metadata

    # 讀取 config.yaml 獲取書名與章節範圍
    book_title = "有聲小說全集"
    start_chap, end_chap = 1, 2400
    config_path = os.path.join(SRC_DIR, "..", "config.yaml")
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
                if cfg:
                    book_title = cfg.get("book_title", book_title)
                    chaps = cfg.get("selected_indices", [])
                    if chaps:
                        start_chap = chaps[0]
                        end_chap = chaps[-1]
        except Exception as e:
            logging.warning(f"Could not load config.yaml: {e}")

    # 生成並寫入 Workspace/{book_title}/ 目錄下
    meta_info = save_book_metadata(book_title, start_chap, end_chap)
    video_title = meta_info["title"]
    video_desc = meta_info["description"]
    cover_path = meta_info["cover_file"]

    logging.info("\n" + "="*60)
    logging.info(f"📌 [自動生成影片標題]: {video_title}")
    logging.info(f"🖼️ [自動生成影片封面]: {cover_path}")
    logging.info(f"📝 [自動生成影片簡介]:\n{video_desc}")
    logging.info("="*60 + "\n")

    logging.info(f"🚀 啟動加速推流模式，Target Run ID: {args.run_id}")

    artifact_names = get_run_artifact_names(args.run_id, args.repo)
    if not artifact_names:
        logging.error(f"No video-worker-* artifacts found for run {args.run_id}")
        sys.exit(1)

    logging.info(f"找到 {len(artifact_names)} 個 Worker Artifacts 待推流")

    temp_dir = os.path.abspath("temp_stream_workspace")

    total_streamed = 0

    for idx, artifact_name in enumerate(artifact_names):
        logging.info(f"\n==================================================")
        logging.info(f"📦 [{idx+1}/{len(artifact_names)}] 下載 Artifact: {artifact_name}")
        logging.info(f"==================================================")

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)

        dl_cmd = [
            "gh", "run", "download", str(args.run_id),
            "--repo", args.repo,
            "--name", artifact_name,
            "--dir", temp_dir
        ]
        dl_res = subprocess.run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if dl_res.returncode != 0:
            logging.error(f"Failed to download artifact {artifact_name}: {dl_res.stderr}")
            continue

        mp4_files = glob.glob(os.path.join(temp_dir, "**", "*.mp4"), recursive=True)
        mp4_files.sort(key=parse_chapter_number)

        logging.info(f"在 {artifact_name} 中找到 {len(mp4_files)} 個章節影片")

        for mp4 in mp4_files:
            success = stream_file_to_rtmp(mp4, rtmp_url)
            if success:
                total_streamed += 1

        shutil.rmtree(temp_dir, ignore_errors=True)
        logging.info(f"🧹 已清理 {artifact_name} 的硬碟暫存")

    logging.info(f"\n🎉 全數推流完畢！共傳送 {total_streamed} 章影片至 YouTube Live！")

if __name__ == "__main__":
    main()
