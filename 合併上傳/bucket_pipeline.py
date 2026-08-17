"""Move GitHub run artifacts through an HF Bucket and finalize inside an HF Job."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

import requests
from huggingface_hub import batch_bucket_files

from merge_upload import chapter_number, merge, normalize_book_title, normalize_run_id, now, ordered_chapter_videos


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


def stage_worker(args):
    artifacts = [item for item in all_artifacts(args.repository, args.run_id) if item["name"] == args.worker_name]
    if not artifacts: raise RuntimeError(f"Artifact {args.worker_name!r} is missing or expired")
    prefix = f"runs/{args.run_id}/workers"
    with tempfile.TemporaryDirectory() as temp_name:
        temp = Path(temp_name); download_artifact(artifacts[0], temp / "files")
        videos = ordered_chapter_videos(temp / "files"); output = temp / f"{args.worker_name}.mp4"
        merge(videos, output, temp / "chapters.ffconcat")
        manifest = {"worker": args.worker_name, "first_chapter": chapter_number(videos[0]), "last_chapter": chapter_number(videos[-1]), "chapters": [chapter_number(path) for path in videos], "remote_file": f"{prefix}/{args.worker_name}.mp4", "completed_at": now()}
        manifest_path = temp / f"{args.worker_name}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        batch_bucket_files(args.bucket, add=[(str(output), manifest["remote_file"]), (str(manifest_path), f"{prefix}/{args.worker_name}.json")])
        if args.stage_metadata:
            artifacts_by_name = {item["name"]: item for item in all_artifacts(args.repository, args.run_id)}
            metadata_additions = []
            for artifact_name in ("shared-config", "source-book-metadata"):
                artifact = artifacts_by_name.get(artifact_name)
                if not artifact:
                    if artifact_name == "shared-config": raise RuntimeError("Source run is missing shared-config")
                    continue
                destination = temp / artifact_name; download_artifact(artifact, destination)
                for path in destination.rglob("*"):
                    if path.is_file(): metadata_additions.append((str(path), f"runs/{args.run_id}/metadata/{artifact_name}/{path.relative_to(destination).as_posix()}"))
            batch_bucket_files(args.bucket, add=metadata_additions)
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


def source_metadata(run_root):
    metadata_root = run_root / "metadata"
    config_path = next((path for path in (metadata_root / "shared-config").rglob("config.yaml")), None)
    cover_path = next((path for path in (metadata_root / "source-book-metadata").rglob("youtube_cover.jpg")), None)
    if not config_path: raise RuntimeError("Source metadata has no config.yaml")
    import yaml
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    title = normalize_book_title(config.get("book_title", ""))
    if not cover_path:
        cover_path = metadata_root / "youtube_cover.jpg"
        from merge_upload import Pipeline
        Pipeline.download_existing_youtube_cover(title, cover_path)
    return title, cover_path


def finalize(args):
    run_root = args.bucket_mount / "runs" / args.run_id; worker_root = run_root / "workers"
    manifests = [json.loads(path.read_text(encoding="utf-8")) for path in worker_root.glob("mp4-worker-*.json")]
    manifests.sort(key=lambda item: item["first_chapter"])
    if len(manifests) != args.expected_workers: raise RuntimeError(f"Expected {args.expected_workers} worker manifests, found {len(manifests)}")
    videos = [args.bucket_mount / item["remote_file"] for item in manifests]
    missing = [str(path) for path in videos if not path.is_file()]
    if missing: raise RuntimeError(f"Missing worker videos: {missing}")
    output = run_root / "merged" / "merged-audiobook.mp4"; output.parent.mkdir(parents=True, exist_ok=True)
    merge(videos, output, run_root / "workers.ffconcat")
    chapters = [chapter for item in manifests for chapter in item["chapters"]]
    title, cover = source_metadata(run_root)
    from src.metadata_gen import generate_video_description, generate_video_title
    from src.youtube_api_uploader import get_authenticated_service, upload_video_file
    youtube_title = generate_video_title(title, min(chapters), max(chapters)); description = generate_video_description(title, min(chapters), max(chapters))
    video_id = upload_video_file(get_authenticated_service(), str(output), youtube_title, description, privacy_status=args.privacy, cover_path=str(cover))
    state = {"status": "complete", "source_run_id": args.run_id, "completed_at": now(), "video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}", "merged_file": str(output.relative_to(args.bucket_mount)), "chapters": len(chapters)}
    (run_root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False), flush=True)


def parse_args():
    parser = argparse.ArgumentParser(); parser.add_argument("--repository", required=True); parser.add_argument("--run-id", required=True, type=normalize_run_id)
    parser.add_argument("--bucket", default=""); parser.add_argument("--worker-name", default=""); parser.add_argument("--bucket-mount", type=Path)
    parser.add_argument("--stage-metadata", action="store_true")
    parser.add_argument("--expected-workers", type=int, default=0); parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private")
    return parser.parse_args()


def main():
    args = parse_args()
    if shutil.which("ffmpeg") is None: raise RuntimeError("ffmpeg is required")
    try:
        if args.worker_name: stage_worker(args)
        elif args.bucket_mount: finalize(args)
        else: raise RuntimeError("Specify --worker-name or --bucket-mount")
        return 0
    except Exception as error:
        print(json.dumps({"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2), file=sys.stderr); return 1


if __name__ == "__main__": sys.exit(main())
