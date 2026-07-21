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
    logging.info(f"▶️ Streaming to YouTube: {os.path.basename(mp4_path)}")
    # Use ffmpeg to stream MP4 to RTMP with fast copy or re-encode audio if needed
    cmd = [
        "ffmpeg",
        "-re",
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
        logging.error(f"❌ Streaming failed for {mp4_path}: {process.stderr[-500:]}")
        return False
    logging.info(f"✅ Finished streaming: {os.path.basename(mp4_path)}")
    return True

def get_run_artifact_names(run_id, repo):
    cmd = ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts", "--jq", ".artifacts[].name"]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        logging.error(f"Failed to fetch artifacts for run {run_id}: {res.stderr}")
        return []
    names = [n.strip() for n in res.stdout.splitlines() if n.strip().startswith("video-worker-")]
    # Sort by worker number
    names.sort(key=lambda x: int(re.search(r'\d+', x).group()) if re.search(r'\d+', x) else 999)
    return names

def main():
    parser = argparse.ArgumentParser(description="Stream GitHub Actions Artifacts to YouTube Live")
    parser.add_argument("--run-id", required=True, help="GitHub Actions Run ID (e.g. 29821206020)")
    parser.add_argument("--repo", default="hub-google/audiobook-generator", help="GitHub Repository")
    args = parser.parse_args()

    stream_key = os.environ.get("YOUTUBE_STREAM_KEY")
    if not stream_key:
        logging.error("CRITICAL: YOUTUBE_STREAM_KEY environment variable is missing!")
        sys.exit(1)

    rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    logging.info(f"🚀 Initializing YouTube Live Stream for Run ID: {args.run_id}")

    artifact_names = get_run_artifact_names(args.run_id, args.repo)
    if not artifact_names:
        logging.error(f"No video-worker-* artifacts found for run {args.run_id}")
        sys.exit(1)

    logging.info(f"Found {len(artifact_names)} worker artifacts to stream: {artifact_names}")

    temp_dir = os.path.abspath("temp_stream_workspace")

    total_streamed = 0

    for idx, artifact_name in enumerate(artifact_names):
        logging.info(f"\n==================================================")
        logging.info(f"📦 [{idx+1}/{len(artifact_names)}] Downloading artifact: {artifact_name}")
        logging.info(f"==================================================")

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)

        # Download artifact using gh CLI
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

        # Find all MP4 files
        mp4_files = glob.glob(os.path.join(temp_dir, "**", "*.mp4"), recursive=True)
        mp4_files.sort(key=parse_chapter_number)

        logging.info(f"Found {len(mp4_files)} MP4 chapters in {artifact_name}")

        for mp4 in mp4_files:
            success = stream_file_to_rtmp(mp4, rtmp_url)
            if success:
                total_streamed += 1

        # Clean up temporary directory to save disk space
        shutil.rmtree(temp_dir, ignore_errors=True)
        logging.info(f"🧹 Cleaned up disk space for {artifact_name}")

    logging.info(f"\n🎉 Streaming Completed! Total {total_streamed} chapter videos streamed to YouTube Live.")

if __name__ == "__main__":
    main()
