"""Move GitHub run artifacts through an HF Bucket and finalize inside an HF Job."""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import zipfile
from pathlib import Path

import requests

# GitHub Actions invokes this file by path, which otherwise puts only this
# directory (not the repository root containing ``src``) on sys.path.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from merge_upload import merge, normalize_book_title, normalize_run_id, now, ordered_chapter_videos

WORKER_RE = re.compile(r"mp4-worker-(\d+)$")


def gh_headers():
    return {"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def all_artifacts(repository, run_id):
    result, page = [], 1
    while True:
        url = f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100&page={page}"
        response = requests.get(url, headers=gh_headers(), timeout=60); response.raise_for_status()
        batch = response.json().get("artifacts", [])
        result.extend(item for item in batch if not item.get("expired"))
        if len(batch) < 100: return result
        page += 1


def download_artifact(artifact, destination):
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "artifact.zip"
    with requests.get(artifact["archive_download_url"], headers=gh_headers(), stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        with archive.open("wb") as handle:
            for block in response.iter_content(8 * 1024 * 1024):
                if block: handle.write(block)
    with zipfile.ZipFile(archive) as source: source.extractall(destination)
    archive.unlink()


def source_metadata_from_github(artifacts, temp):
    artifacts_by_name = {item["name"]: item for item in artifacts}
    metadata_root = temp / "metadata"
    config_artifact = artifacts_by_name.get("shared-config")
    if not config_artifact: raise RuntimeError("Source run is missing shared-config")
    download_artifact(config_artifact, metadata_root / "shared-config")
    cover_artifact = artifacts_by_name.get("source-book-metadata")
    if cover_artifact: download_artifact(cover_artifact, metadata_root / "source-book-metadata")
    config_path = next((path for path in (metadata_root / "shared-config").rglob("config.yaml")), None)
    cover_path = next((path for path in (metadata_root / "source-book-metadata").rglob("youtube_cover.jpg")), None)
    if not config_path: raise RuntimeError("Source metadata has no config.yaml")
    import yaml
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    title = normalize_book_title(config.get("book_title", ""))
    end_chapter = int(config.get("end_chapter") or len(config.get("selected_indices") or config.get("chapters") or []))
    if not cover_path:
        cover_path = metadata_root / "youtube_cover.jpg"
        from merge_upload import Pipeline
        Pipeline.download_existing_youtube_cover(title, cover_path)
    return title, cover_path, end_chapter


class ArtifactVideoProvider:
    """Materialize one GitHub artifact at a time and serve it with HTTP ranges."""
    def __init__(self, artifacts, root):
        self.artifacts = artifacts; self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.current_index = None; self.current_file = None; self.lock = threading.Lock()

    def materialize(self, index):
        with self.lock:
            if self.current_index == index and self.current_file and self.current_file.is_file(): return self.current_file
            if self.current_file:
                shutil.rmtree(self.current_file.parent, ignore_errors=True)
            work = self.root / f"worker-{index}"; work.mkdir(parents=True, exist_ok=True)
            download_artifact(self.artifacts[index], work / "artifact")
            videos = ordered_chapter_videos(work / "artifact")
            output = work / f"{self.artifacts[index]['name']}.mp4"
            merge(videos, output, work / "chapters.ffconcat")
            shutil.rmtree(work / "artifact", ignore_errors=True)
            self.current_index, self.current_file = index, output
            return output

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _range_handler(provider):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[artifact-stream] {fmt % args}", flush=True)

        def _serve(self, body):
            match = re.fullmatch(r"/worker/(\d+)\.mp4", self.path.split("?", 1)[0])
            if not match: self.send_error(404); return
            index = int(match.group(1))
            if index >= len(provider.artifacts): self.send_error(404); return
            path = provider.materialize(index); size = path.stat().st_size
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get("Range", "")
            range_match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start = int(range_match.group(1)); end = int(range_match.group(2) or size - 1); status = 206
                if start >= size or end < start: self.send_error(416); return
                end = min(end, size - 1)
            self.send_response(status); self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(end - start + 1))
            if status == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not body: return
            remaining = end - start + 1
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining:
                    block = handle.read(min(8 * 1024 * 1024, remaining))
                    if not block: break
                    self.wfile.write(block); remaining -= len(block)

        def do_HEAD(self): self._serve(False)
        def do_GET(self): self._serve(True)
    return Handler


def merge_artifacts_to_bucket(worker_artifacts, output, temp):
    provider = ArtifactVideoProvider(worker_artifacts, temp / "artifact-stream")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _range_handler(provider))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    concat_file = temp / "workers.ffconcat"
    concat_file.write_text("ffconcat version 1.0\n" + "".join(
        f"file 'http://127.0.0.1:{server.server_port}/worker/{index}.mp4'\n"
        for index in range(len(worker_artifacts))
    ), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-y", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-f", "concat", "-safe", "0", "-i", str(concat_file), "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(output),
        ], check=True)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=10); provider.cleanup()


def finalize_direct(args):
    artifacts = all_artifacts(args.repository, args.run_id)
    worker_artifacts = [item for item in artifacts if WORKER_RE.fullmatch(item["name"])]
    worker_artifacts.sort(key=lambda item: int(WORKER_RE.fullmatch(item["name"]).group(1)))
    if len(worker_artifacts) != args.expected_workers: raise RuntimeError(f"Expected {args.expected_workers} worker artifacts, found {len(worker_artifacts)}")
    run_root = args.bucket_mount / "runs" / args.run_id
    state_path = run_root / "state.json"
    if state_path.is_file():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if previous.get("status") == "complete":
            print(json.dumps(previous, ensure_ascii=False), flush=True)
            return
    output = run_root / "merged-audiobook.mp4"; output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_name:
        temp = Path(temp_name)
        title, cover, end_chapter = source_metadata_from_github(artifacts, temp)
        if not output.is_file(): merge_artifacts_to_bucket(worker_artifacts, output, temp)
        from src.metadata_gen import generate_video_description, generate_video_title
        from src.youtube_api_uploader import get_authenticated_service, upload_video_file
        youtube_title = generate_video_title(title, 1, end_chapter); description = generate_video_description(title, 1, end_chapter)
        video_id = upload_video_file(get_authenticated_service(), str(output), youtube_title, description, privacy_status=args.privacy, cover_path=str(cover))
        state = {"status": "complete", "source_run_id": args.run_id, "completed_at": now(), "video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}", "chapters": end_chapter}
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    output.unlink()
    print(json.dumps(state, ensure_ascii=False), flush=True)


def parse_args():
    parser = argparse.ArgumentParser(); parser.add_argument("--repository", required=True); parser.add_argument("--run-id", required=True, type=normalize_run_id)
    parser.add_argument("--bucket-mount", type=Path, required=True)
    parser.add_argument("--expected-workers", type=int, default=0)
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private")
    return parser.parse_args()


def main():
    args = parse_args()
    if shutil.which("ffmpeg") is None: raise RuntimeError("ffmpeg is required")
    try:
        finalize_direct(args)
        return 0
    except Exception as error:
        print(json.dumps({"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2), file=sys.stderr); return 1


if __name__ == "__main__": sys.exit(main())
