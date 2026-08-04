"""
download_all_and_merge_single_mp4.py — 包含即時進度顯示 (Real-time Progress Logging) 的全書單一 MP4 下載與無損合成腳本
"""
import os
import sys
import glob
import json
import shutil
import time
import subprocess
import logging

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Single-MP4-Builder] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

def get_run_artifacts(run_id, repo="hub-google/audiobook-generator"):
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts", "--paginate", "--jq", ".artifacts[] | {id:.id, name:.name, size:.size_in_bytes}"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"無法獲取 Run #{run_id} 的 Artifacts: {res.stderr}")
        return []
    artifacts = []
    for line in res.stdout.strip().splitlines():
        if line.strip():
            artifacts.append(json.loads(line))
    artifacts = [a for a in artifacts if "video-worker" in a["name"] or "mp4-worker" in a["name"]]
    return artifacts

def download_run_artifacts_with_progress(run_id="29963057199", repo="hub-google/audiobook-generator", temp_dir="temp_single_merge_ws"):
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
    os.makedirs(temp_dir, exist_ok=True)

    artifacts = get_run_artifacts(run_id, repo)
    if not artifacts:
        logging.error(f"❌ 在 Run #{run_id} 中未找到任何 Worker 產出物！")
        return False

    total_count = len(artifacts)
    total_bytes = sum(a["size"] for a in artifacts)
    total_gb = total_bytes / (1024 * 1024 * 1024)

    print("=" * 60)
    print(f"📊 【開始抓取任務】 Run #{run_id} 共包含 {total_count} 個 Worker 產物包 (總容量: {total_gb:.2f} GB)")
    print("=" * 60)
    sys.stdout.flush()

    for idx, art in enumerate(artifacts, 1):
        art_name = art["name"]
        art_size_mb = art["size"] / (1024 * 1024)
        pct = (idx / total_count) * 100

        print(f"📥 [{idx}/{total_count}] ({pct:.1f}%) 正在下載產物包: {art_name} ({art_size_mb:.1f} MB)...")
        sys.stdout.flush()

        start_time = time.time()
        dl_cmd = [
            "gh", "run", "download", str(run_id),
            "--repo", repo,
            "--name", art_name,
            "--dir", temp_dir
        ]
        res = subprocess.run(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        elapsed = time.time() - start_time

        if res.returncode == 0:
            dl_speed = art_size_mb / (elapsed if elapsed > 0 else 1)
            print(f"✅ [{idx}/{total_count}] ({pct:.1f}%) {art_name} 下載完成！耗時 {elapsed:.1f}s (速度: {dl_speed:.1f} MB/s)")
        else:
            print(f"❌ [{idx}/{total_count}] 下載 {art_name} 失敗: {res.stderr}")

        sys.stdout.flush()

    print("=" * 60)
    print("🎉 所有 20 個 Worker 產出包已全數下載完畢！開始進入 FFmpeg 無損合成階段...")
    print("=" * 60)
    sys.stdout.flush()
    return True

def merge_and_clean(temp_dir="temp_single_merge_ws", output_dir="Output"):
    from test_merge_all import merge_all_mp4s

    print("🎬 【FFmpeg 無損合成中】正在將 2442 章 MP4 一次性拼接為「單一超長 Full Book MP4」...")
    sys.stdout.flush()

    success = merge_all_mp4s(temp_dir, output_dir)

    if success:
        print("🧹 【自動清理】全書單一 MP4 合成成功！正在自動刪除所有單章暫存檔...")
        sys.stdout.flush()
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists("Downloaded_MP4s"):
            shutil.rmtree("Downloaded_MP4s", ignore_errors=True)
        print("✨ 【完成】電腦上已完成並僅保留唯一一個 2442 章全集超長 MP4 影片！")
        sys.stdout.flush()

    return success

if __name__ == "__main__":
    run_id = sys.argv[1] if len(sys.argv) > 1 else "29963057199"
    if download_run_artifacts_with_progress(run_id):
        merge_and_clean()
