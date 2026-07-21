import os
import sys
import glob
import re
import shutil
import subprocess
import argparse
import logging
import threading

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

def stream_merged_file_to_rtmp(video_path, rtmp_url):
    filename = os.path.basename(video_path)
    logging.info(f"▶️ [極速大頻寬推流] 傳送至 YouTube Live: {filename}")
    
    cmd = [
        "ffmpeg",
        "-re",  # 保障直播穩定
        "-i", video_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-flvflags", "no_duration_filesize",
        "-max_muxing_queue_size", "2048",
        "-f", "flv",
        rtmp_url
    ]
    process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if process.returncode != 0:
        logging.error(f"❌ 傳送失敗 [{filename}]: {process.stderr[-500:]}")
        return False
    logging.info(f"✅ 完成推流: {filename}")
    return True

def concat_mp4_files(mp4_files, output_merged_path):
    """將同一個 Worker 的所有 MP4 極速無損 (-c copy) 合併為單一影片檔 (不到 1 秒完成)"""
    if len(mp4_files) == 1:
        shutil.copy(mp4_files[0], output_merged_path)
        return True

    concat_txt = os.path.join(os.path.dirname(output_merged_path), "concat_list.txt")
    with open(concat_txt, "w", encoding="utf-8") as f:
        for mp4 in mp4_files:
            safe_p = os.path.abspath(mp4).replace("'", "'\\''")
            f.write(f"file '{safe_p}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_txt,
        "-c", "copy",
        output_merged_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if os.path.exists(concat_txt):
        os.remove(concat_txt)
    return res.returncode == 0 and os.path.exists(output_merged_path)

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
                logging.info(f"✅ Key [{key_masked}] 檢測結果：狀態【空閒可用】！選定此 Key 進行推流。")
                return key
            else:
                logging.warning(f"⚠️ Key [{key_masked}] 檢測結果：【正在使用中/已被佔用】，自動跳過切換下一組...")
        except Exception as e:
            logging.warning(f"⚠️ Key [{key_masked}] 測試超時或連線異常: {e}，切換下一組...")

    logging.warning("⚠️ 備選池中所有 Key 均回應忙碌，預設使用第一組 Key 執行。")
    return candidate_keys[0]

def download_artifact_task(run_id, repo, artifact_name, dest_dir):
    """背景執行下載任務"""
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    os.makedirs(dest_dir, exist_ok=True)
    
    dl_cmd = [
        "gh", "run", "download", str(run_id),
        "--repo", repo,
        "--name", artifact_name,
        "--dir", dest_dir
    ]
    res = subprocess.run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.returncode == 0

def main():
    parser = argparse.ArgumentParser(description="Accelerated YouTube Live Streamer with Multi-Key Failover & Async Prefetch")
    parser.add_argument("--run-id", required=True, help="GitHub Actions Run ID (e.g. 29821206020)")
    parser.add_argument("--repo", default="hub-google/audiobook-generator", help="GitHub Repository")
    args = parser.parse_args()

    candidate_keys = get_candidate_stream_keys()
    if not candidate_keys:
        logging.error("CRITICAL: No YOUTUBE_STREAM_KEY environment variables found!")
        sys.exit(1)

    selected_key = test_and_select_stream_key(candidate_keys)
    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{selected_key}"

    SRC_DIR = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, SRC_DIR)
    from metadata_gen import save_book_metadata

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

    meta_info = save_book_metadata(book_title, start_chap, end_chap)
    video_title = meta_info["title"]
    video_desc = meta_info["description"]
    cover_path = meta_info["cover_file"]

    logging.info("\n" + "="*60)
    logging.info(f"📌 [自動生成影片標題]: {video_title}")
    logging.info(f"🖼️ [自動生成影片封面]: {cover_path}")
    logging.info(f"📝 [自動生成影片簡介]:\n{video_desc}")
    logging.info("="*60 + "\n")

    logging.info(f"🚀 啟動異步預下載 (Async Prefetch) 與加速推流模式，Target Run ID: {args.run_id}")

    artifact_names = get_run_artifact_names(args.run_id, args.repo)
    if not artifact_names:
        logging.error(f"No video-worker-* artifacts found for run {args.run_id}")
        sys.exit(1)

    logging.info(f"找到 {len(artifact_names)} 個 Worker Artifacts，開啟背景雙線程流水線...")

    base_temp = os.path.abspath("temp_stream_workspace")
    dir_a = os.path.join(base_temp, "dir_a")
    dir_b = os.path.join(base_temp, "dir_b")

    total_streamed = 0

    # 啟動第一個 Artifact 的背景下載
    curr_dir = dir_a
    next_dir = dir_b

    logging.info(f"📦 [1/{len(artifact_names)}] 開始下載 Artifact: {artifact_names[0]}...")
    next_thread = threading.Thread(
        target=download_artifact_task, 
        args=(args.run_id, args.repo, artifact_names[0], curr_dir)
    )
    next_thread.start()

    for idx, artifact_name in enumerate(artifact_names):
        # 等待目前的 Artifact 下載完成
        next_thread.join()

        # 立即發起【下一個 Artifact】的背景預下載線程 (若還有下一個)
        if idx + 1 < len(artifact_names):
            next_artifact = artifact_names[idx + 1]
            logging.info(f"⚡ 啟動背景線程【預先下載】下一個 Artifact [{idx+2}/{len(artifact_names)}]: {next_artifact}")
            next_thread = threading.Thread(
                target=download_artifact_task,
                args=(args.run_id, args.repo, next_artifact, next_dir)
            )
            next_thread.start()
        else:
            next_thread = None

        logging.info(f"\n==================================================")
        logging.info(f"🎥 [{idx+1}/{len(artifact_names)}] 正式推流處理: {artifact_name}")
        logging.info(f"==================================================")

        mp4_files = glob.glob(os.path.join(curr_dir, "**", "*.mp4"), recursive=True)
        mp4_files.sort(key=parse_chapter_number)

        if mp4_files:
            logging.info(f"在 {artifact_name} 中找到 {len(mp4_files)} 個章節影片，進行秒級 concat 合併為單一串流...")
            merged_worker_mp4 = os.path.join(curr_dir, f"{artifact_name}_merged.mp4")
            if concat_mp4_files(mp4_files, merged_worker_mp4):
                if stream_merged_file_to_rtmp(merged_worker_mp4, rtmp_url):
                    total_streamed += len(mp4_files)
            else:
                logging.warning("⚠️ Concat 合併失敗，降階為單檔個別推流模式...")
                for mp4 in mp4_files:
                    if stream_merged_file_to_rtmp(mp4, rtmp_url):
                        total_streamed += 1
        else:
            logging.warning(f"⚠️ {artifact_name} 中未找到任何 .mp4 檔案")

        # 串流結束後，清理目前資料夾並交換雙緩衝區
        shutil.rmtree(curr_dir, ignore_errors=True)
        curr_dir, next_dir = next_dir, curr_dir

    logging.info(f"\n🎉 全數推流完畢！共傳送 {total_streamed} 章影片至 YouTube Live！")

if __name__ == "__main__":
    main()
