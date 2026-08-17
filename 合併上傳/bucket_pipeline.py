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

from merge_upload import Pipeline, chapter_number, merge, normalize_book_title, normalize_run_id, now, ordered_chapter_videos
from src.part_builder import merge_part_videos
from src.source_status import confirmed_missing_from_directory

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
    if cover_artifact and not cover_path:
        raise RuntimeError(
            "source-book-metadata contains no preserved youtube_cover.jpg; refusing to continue"
        )
    if not cover_path:
        # Runs created before source-book-metadata was introduced have no
        # preserved cover artifact. Reuse the exact thumbnail already present
        # on the authenticated owner's YouTube channel; never generate one.
        cover_path = metadata_root / "youtube_cover.jpg"
        Pipeline.download_existing_youtube_cover(title, cover_path)
    return title, cover_path, end_chapter, config


def source_config_from_github(artifacts, temp):
    config_artifact = next((item for item in artifacts if item["name"] == "shared-config"), None)
    if not config_artifact:
        raise RuntimeError("Source run is missing shared-config")
    root = Path(temp) / "shared-config"
    download_artifact(config_artifact, root)
    config_path = next(root.rglob("config.yaml"), None)
    if not config_path:
        raise RuntimeError("Source metadata has no config.yaml")
    import yaml
    return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}


def run_preflight(args):
    """Resolve and persist cheap mandatory inputs before any shard work starts."""
    run_root = args.bucket_mount / "runs" / args.run_id
    metadata_root = run_root / "metadata"
    report_path = metadata_root / "report.json"
    report = {
        "status": "running", "stage": "validating_source_metadata",
        "source_run_id": args.run_id, "started_at": now(),
    }
    write_report(report_path, report)
    try:
        artifacts = all_artifacts(args.repository, args.run_id)
        with tempfile.TemporaryDirectory() as temp_name:
            title, cover, end_chapter, config = source_metadata_from_github(
                artifacts, Path(temp_name)
            )
            if not title:
                raise RuntimeError("Source config has no usable book_title")
            if not cover.is_file() or cover.stat().st_size <= 0:
                raise RuntimeError("Resolved YouTube cover is empty")
            from src.metadata_gen import generate_video_description, generate_video_title
            youtube_title = generate_video_title(title, 1, end_chapter)
            description = generate_video_description(title, 1, end_chapter)
            if not youtube_title.strip() or not description.strip():
                raise RuntimeError("Generated YouTube title or description is empty")
            metadata_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cover, metadata_root / "youtube_cover.jpg")
            (metadata_root / "config.yaml").write_text(
                __import__("yaml").safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            metadata = {
                "status": "complete", "source_run_id": args.run_id,
                "book_title": title, "end_chapter": end_chapter,
                "youtube_title": youtube_title, "description": description,
                "cover_bytes": (metadata_root / "youtube_cover.jpg").stat().st_size,
                "completed_at": now(),
            }
            write_report(metadata_root / "metadata.json", metadata)
            report.update(metadata)
            report["stage"] = "metadata_ready"
            write_report(report_path, report)
    except Exception as error:
        report.update({
            "status": "failed", "failed_at": now(),
            "error_type": type(error).__name__, "error": str(error),
        })
        write_report(report_path, report)
        raise


def load_preflight_metadata(run_root):
    metadata_root = Path(run_root) / "metadata"
    metadata_path = metadata_root / "metadata.json"
    config_path = metadata_root / "config.yaml"
    cover_path = metadata_root / "youtube_cover.jpg"
    if not metadata_path.is_file():
        raise RuntimeError("metadata preflight did not complete")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("status") != "complete":
        raise RuntimeError("metadata preflight is not complete")
    if not config_path.is_file():
        raise RuntimeError("metadata preflight config.yaml is missing")
    if not cover_path.is_file() or cover_path.stat().st_size <= 0:
        raise RuntimeError("metadata preflight YouTube cover is missing or empty")
    import yaml
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    title = normalize_book_title(config.get("book_title", ""))
    end_chapter = int(config.get("end_chapter") or len(config.get("selected_indices") or config.get("chapters") or []))
    if title != metadata.get("book_title") or end_chapter != int(metadata.get("end_chapter") or 0):
        raise RuntimeError("metadata preflight files are inconsistent")
    youtube_title = str(metadata.get("youtube_title") or "")
    description = str(metadata.get("description") or "")
    if not youtube_title.strip() or not description.strip():
        raise RuntimeError("metadata preflight title or description is missing")
    return title, cover_path, end_chapter, config, youtube_title, description


def expected_worker_chapters(config, worker_id):
    selected = [int(value) for value in (config.get("selected_indices") or [])]
    per_worker = int(config.get("chapters_per_worker") or 0)
    if not selected or per_worker <= 0:
        raise RuntimeError("config.yaml must contain selected_indices and a positive chapters_per_worker")
    start = int(worker_id) * per_worker
    return selected[start:start + per_worker]


def validate_worker_inventory(worker_name, expected, actual, confirmed_missing):
    expected = [int(value) for value in expected]
    actual = [int(value) for value in actual]
    confirmed_missing = {int(value) for value in confirmed_missing}
    duplicates = sorted({chapter for chapter in actual if actual.count(chapter) > 1})
    unexpected = sorted(set(actual) - set(expected))
    invalid_missing_evidence = sorted(confirmed_missing - set(expected))
    unresolved = sorted(set(expected) - set(actual) - confirmed_missing)
    overlap = sorted(set(actual) & confirmed_missing)
    if duplicates or unexpected or invalid_missing_evidence or unresolved or overlap:
        raise RuntimeError(
            f"{worker_name} chapter inventory mismatch: duplicates={duplicates}, "
            f"unexpected={unexpected}, unresolved_missing={unresolved}, "
            f"invalid_source_missing={invalid_missing_evidence}, mp4_and_missing_overlap={overlap}"
        )
    return {
        "expected": expected,
        "mp4_chapters": sorted(actual),
        "source_missing_chapters": sorted(set(expected) & confirmed_missing),
    }


def inventory_worker_artifacts(worker_artifacts, config, temp, on_progress=None):
    selected = config.get("selected_indices") or []
    per_worker = int(config.get("chapters_per_worker") or 0)
    if not selected or per_worker <= 0:
        raise RuntimeError("config.yaml must contain selected_indices and a positive chapters_per_worker")
    expected_worker_count = (
        len(selected) + per_worker - 1
    ) // per_worker
    worker_ids = [int(WORKER_RE.fullmatch(item["name"]).group(1)) for item in worker_artifacts]
    if worker_ids != list(range(expected_worker_count)):
        raise RuntimeError(
            f"worker artifacts are incomplete: expected IDs={list(range(expected_worker_count))}, "
            f"found IDs={worker_ids}"
        )

    inventory = []
    inventory_root = temp / "inventory"
    for artifact, worker_id in zip(worker_artifacts, worker_ids):
        extracted = inventory_root / artifact["name"]
        download_artifact(artifact, extracted)
        videos = [
            path for path in extracted.rglob("*.mp4")
            if re.search(r"chapter_(\d+)", path.name)
        ]
        actual = [chapter_number(path) for path in videos]
        missing = confirmed_missing_from_directory(extracted)
        expected = expected_worker_chapters(config, worker_id)
        try:
            record = validate_worker_inventory(artifact["name"], expected, actual, missing)
        except Exception as error:
            if on_progress:
                on_progress({
                    "worker_id": worker_id, "expected": expected,
                    "mp4_chapters": sorted(actual),
                    "source_missing_chapters": sorted(missing),
                    "validation_error": str(error),
                })
            raise
        record.update({"worker_id": worker_id, "artifact": artifact})
        inventory.append(record)
        if on_progress:
            on_progress({key: value for key, value in record.items() if key != "artifact"})
        shutil.rmtree(extracted, ignore_errors=True)
    return inventory


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def public_inventory(record):
    return {key: value for key, value in record.items() if key not in ("artifact", "videos")}


def run_shard(args):
    run_root = args.bucket_mount / "runs" / args.run_id
    shard_root = run_root / "shards" / f"shard-{args.shard_id}"
    report_path = shard_root / "report.json"
    # A rerun owns this shard directory and must never reuse a partial chunk.
    shard_root.mkdir(parents=True, exist_ok=True)
    for stale_name in ("chunk.mp4", "manifest.json", "report.json", "report.tmp"):
        stale_path = shard_root / stale_name
        if stale_path.is_file():
            stale_path.unlink()
    report = {
        "status": "running", "stage": "discovering", "source_run_id": args.run_id,
        "shard_id": args.shard_id, "worker_start": args.worker_start,
        "worker_end": args.worker_end, "worker_inventory": [], "started_at": now(),
    }
    write_report(report_path, report)
    try:
        artifacts = all_artifacts(args.repository, args.run_id)
        workers = sorted(
            (item for item in artifacts if WORKER_RE.fullmatch(item["name"])),
            key=lambda item: int(WORKER_RE.fullmatch(item["name"]).group(1)),
        )
        if len(workers) != args.expected_workers:
            raise RuntimeError(f"Expected {args.expected_workers} worker artifacts, found {len(workers)}")
        worker_ids = [int(WORKER_RE.fullmatch(item["name"]).group(1)) for item in workers]
        if worker_ids != list(range(args.expected_workers)):
            raise RuntimeError(
                f"worker artifacts are incomplete: expected IDs={list(range(args.expected_workers))}, "
                f"found IDs={worker_ids}"
            )
        assigned = workers[args.worker_start:args.worker_end]
        if not assigned:
            raise RuntimeError("shard has no assigned worker artifacts")

        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            _title, _cover, _end_chapter, config, _youtube_title, _description = load_preflight_metadata(run_root)
            report["stage"] = "inventory"
            write_report(report_path, report)
            records, all_videos = [], []
            for artifact in assigned:
                worker_id = int(WORKER_RE.fullmatch(artifact["name"]).group(1))
                extracted = temp / "workers" / artifact["name"]
                download_artifact(artifact, extracted)
                videos = ordered_chapter_videos(extracted) if any(
                    re.search(r"chapter_(\d+)", path.name) for path in extracted.rglob("*.mp4")
                ) else []
                actual = [chapter_number(path) for path in videos]
                missing = confirmed_missing_from_directory(extracted)
                expected = expected_worker_chapters(config, worker_id)
                try:
                    record = validate_worker_inventory(artifact["name"], expected, actual, missing)
                except Exception as error:
                    report["worker_inventory"].append({
                        "worker_id": worker_id, "expected": expected,
                        "mp4_chapters": sorted(actual),
                        "source_missing_chapters": sorted(missing),
                        "validation_error": str(error),
                    })
                    write_report(report_path, report)
                    raise
                record.update({"worker_id": worker_id, "artifact": artifact, "videos": videos})
                records.append(record)
                all_videos.extend(videos)
                report["worker_inventory"].append(public_inventory(record))
                write_report(report_path, report)

            all_videos.sort(key=lambda path: (chapter_number(path), str(path)))
            merged_chapters = [chapter_number(path) for path in all_videos]
            report.update({"stage": "merging", "planned_merge_chapters": merged_chapters})
            write_report(report_path, report)
            shard_root.mkdir(parents=True, exist_ok=True)
            chunk_path = None
            chunk_bytes = 0
            if all_videos:
                local_chunk = temp / f"chunk-{args.shard_id}.mp4"
                merged_ok = merge_part_videos({
                    "part_num": args.shard_id,
                    "files": [str(path) for path in all_videos],
                }, str(local_chunk))
                if not merged_ok or not local_chunk.is_file() or local_chunk.stat().st_size <= 0:
                    raise RuntimeError("FFmpeg did not produce a non-empty shard chunk")
                chunk_path = shard_root / "chunk.mp4"
                shutil.copyfile(local_chunk, chunk_path)
                chunk_bytes = chunk_path.stat().st_size
            manifest = {
                "status": "complete", "source_run_id": args.run_id,
                "shard_id": args.shard_id,
                "worker_ids": [item["worker_id"] for item in records],
                "worker_inventory": [public_inventory(item) for item in records],
                "merged_chapters": merged_chapters,
                "chunk_file": "chunk.mp4" if chunk_path else None,
                "chunk_bytes": chunk_bytes,
                "completed_at": now(),
            }
            write_report(shard_root / "manifest.json", manifest)
            report.update({
                "status": "complete", "stage": "chunk_saved_to_hf",
                "merged_chapters": merged_chapters, "chunk_bytes": chunk_bytes,
                "completed_at": now(),
            })
            write_report(report_path, report)
    except Exception as error:
        report.update({
            "status": "failed", "failed_at": now(),
            "error_type": type(error).__name__, "error": str(error),
        })
        write_report(report_path, report)
        raise


def load_shard_manifests(run_root, expected_shards):
    manifests = []
    for shard_id in range(expected_shards):
        shard_root = run_root / "shards" / f"shard-{shard_id}"
        manifest_path = shard_root / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"missing completed manifest for shard {shard_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "complete" or int(manifest.get("shard_id", -1)) != shard_id:
            raise RuntimeError(f"invalid manifest for shard {shard_id}")
        chunk = None
        if manifest.get("merged_chapters"):
            chunk = shard_root / str(manifest.get("chunk_file") or "chunk.mp4")
            if not chunk.is_file() or chunk.stat().st_size != int(manifest.get("chunk_bytes") or -1):
                raise RuntimeError(f"HF chunk is missing or has wrong size for shard {shard_id}")
        manifest["chunk_path"] = chunk
        manifests.append(manifest)
    return manifests


def validate_final_manifests(manifests, config, expected_workers):
    worker_ids = [worker for manifest in manifests for worker in manifest["worker_ids"]]
    if worker_ids != list(range(expected_workers)):
        raise RuntimeError(
            f"shard worker coverage mismatch: expected={list(range(expected_workers))}, actual={worker_ids}"
        )
    inventory = [item for manifest in manifests for item in manifest["worker_inventory"]]
    expected = [int(value) for value in config.get("selected_indices") or []]
    merged = [int(value) for manifest in manifests for value in manifest["merged_chapters"]]
    missing = [
        int(value) for item in inventory
        for value in item.get("source_missing_chapters") or []
    ]
    if len(merged) != len(set(merged)):
        raise RuntimeError("duplicate chapters found across shard manifests")
    if sorted(merged + missing) != sorted(expected):
        raise RuntimeError(
            f"final chapter coverage mismatch: expected={expected}, merged={merged}, source_missing={missing}"
        )
    if merged != sorted(merged):
        raise RuntimeError(f"shard chapters are out of order: {merged}")
    return inventory, merged, sorted(missing)


def run_finalize(args):
    run_root = args.bucket_mount / "runs" / args.run_id
    report_path = run_root / "merge-report.json"
    report = {
        "status": "running", "stage": "loading_shards", "source_run_id": args.run_id,
        "worker_inventory": [], "merged_chapters": [], "started_at": now(),
    }
    write_report(report_path, report)
    try:
        state_path = run_root / "state.json"
        if state_path.is_file():
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if previous.get("status") == "complete":
                report.update(previous)
                report.update({"status": "complete", "stage": "already_uploaded"})
                write_report(report_path, report)
                return
        manifests = load_shard_manifests(run_root, args.expected_shards)
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            title, cover, end_chapter, config, youtube_title, description = load_preflight_metadata(run_root)
            inventory, planned, source_missing = validate_final_manifests(
                manifests, config, args.expected_workers
            )
            report.update({
                "stage": "final_merging", "book_title": title,
                "worker_inventory": inventory, "planned_merge_chapters": planned,
                "source_missing_chapters": source_missing,
            })
            write_report(report_path, report)
            output = run_root / "merged-audiobook.mp4"
            chunks = [manifest["chunk_path"] for manifest in manifests if manifest["chunk_path"]]
            if not chunks:
                raise RuntimeError("all selected chapters are source-missing; there is no audiobook to upload")
            merge(chunks, output, temp / "final.ffconcat")
            if not output.is_file() or output.stat().st_size <= 0:
                raise RuntimeError("FFmpeg did not produce the final HF audiobook")
            report.update({
                "stage": "uploading_hf_file", "merged_chapters": planned,
                "output_bytes": output.stat().st_size,
            })
            write_report(report_path, report)
            from src.youtube_api_uploader import get_authenticated_service, upload_video_file
            video_id = upload_video_file(
                get_authenticated_service(), str(output), youtube_title, description,
                privacy_status=args.privacy, cover_path=str(cover),
            )
            state = {
                "status": "complete", "source_run_id": args.run_id, "completed_at": now(),
                "video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}",
                "chapters": end_chapter, "merged_chapters": planned,
                "source_missing_chapters": source_missing, "worker_inventory": inventory,
            }
            write_report(state_path, state)
            report.update(state)
            report["stage"] = "upload_complete"
            write_report(report_path, report)
            output.unlink()
    except Exception as error:
        report.update({
            "status": "failed", "failed_at": now(),
            "error_type": type(error).__name__, "error": str(error),
        })
        write_report(report_path, report)
        raise


def print_summary(report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    status = report.get("status", "unknown")
    icon = "✅" if status == "complete" else "❌" if status == "failed" else "⚠️"
    print(f"{icon} **狀態：{status}**")
    print(f"- 最後階段：`{report.get('stage', 'unknown')}`")
    if report.get("error"):
        print(f"- 錯誤：`{report.get('error_type')}: {report['error']}`")
    print("\n| Worker | 預定章節 | 找到的 MP4 | 原站確認缺失 |\n|---:|---|---|---|")
    show = lambda values: ", ".join(map(str, values)) if values else "—"
    for item in report.get("worker_inventory") or []:
        print(
            f"| {item['worker_id']} | {show(item.get('expected'))} | "
            f"{show(item.get('mp4_chapters'))} | {show(item.get('source_missing_chapters'))} |"
        )
    planned = report.get("planned_merge_chapters") or []
    merged = report.get("merged_chapters") or []
    print(f"\n- 預定送入合併：{len(planned)} 章 — {planned}")
    print(f"- 已確認送入合併：{len(merged)} 章 — {merged}")
    if report.get("video_url"):
        print(f"- YouTube：{report['video_url']}")


class ArtifactVideoProvider:
    """Materialize one GitHub artifact at a time and serve it with HTTP ranges."""
    def __init__(self, inventory, root):
        self.inventory = inventory; self.artifacts = [item["artifact"] for item in inventory]
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.current_index = None; self.current_file = None; self.lock = threading.Lock()
        self.merged_chapters = {}

    def materialize(self, index):
        with self.lock:
            if self.current_index == index and self.current_file and self.current_file.is_file(): return self.current_file
            if self.current_file:
                shutil.rmtree(self.current_file.parent, ignore_errors=True)
            work = self.root / f"worker-{index}"; work.mkdir(parents=True, exist_ok=True)
            download_artifact(self.artifacts[index], work / "artifact")
            videos = ordered_chapter_videos(work / "artifact")
            chapters = [chapter_number(path) for path in videos]
            expected = self.inventory[index]["mp4_chapters"]
            if chapters != expected:
                raise RuntimeError(
                    f"{self.artifacts[index]['name']} changed between inventory and merge: "
                    f"inventoried={expected}, merging={chapters}"
                )
            output = work / f"{self.artifacts[index]['name']}.mp4"
            merge(videos, output, work / "chapters.ffconcat")
            self.merged_chapters[index] = chapters
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


def merge_artifacts_to_bucket(worker_inventory, output, temp):
    mergeable = [item for item in worker_inventory if item["mp4_chapters"]]
    if not mergeable:
        raise RuntimeError("No chapter MP4 files remain after validated source-missing exclusions")
    provider = ArtifactVideoProvider(mergeable, temp / "artifact-stream")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _range_handler(provider))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    concat_file = temp / "workers.ffconcat"
    concat_file.write_text("ffconcat version 1.0\n" + "".join(
        f"file 'http://127.0.0.1:{server.server_port}/worker/{index}.mp4'\n"
        for index in range(len(mergeable))
    ), encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    import subprocess
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-y", "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-f", "concat", "-safe", "0", "-i", str(concat_file), "-map", "0:v:0", "-map", "0:a:0",
            "-c", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", str(output),
        ], check=True)
        actually_merged = [
            chapter for index in range(len(mergeable))
            for chapter in provider.merged_chapters.get(index, [])
        ]
        planned = [chapter for item in mergeable for chapter in item["mp4_chapters"]]
        if actually_merged != planned:
            raise RuntimeError(
                f"final merge did not consume the validated chapter plan: "
                f"planned={planned}, consumed={actually_merged}"
            )
        return actually_merged
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=10); provider.cleanup()


def finalize_direct(args):
    artifacts = all_artifacts(args.repository, args.run_id)
    worker_artifacts = [item for item in artifacts if WORKER_RE.fullmatch(item["name"])]
    worker_artifacts.sort(key=lambda item: int(WORKER_RE.fullmatch(item["name"]).group(1)))
    if len(worker_artifacts) != args.expected_workers: raise RuntimeError(f"Expected {args.expected_workers} worker artifacts, found {len(worker_artifacts)}")
    run_root = args.bucket_mount / "runs" / args.run_id
    state_path = run_root / "state.json"
    report_path = run_root / "merge-report.json"
    report = {
        "status": "running", "stage": "starting", "source_run_id": args.run_id,
        "started_at": now(), "worker_inventory": [], "merged_chapters": [],
    }
    write_report(report_path, report)
    if state_path.is_file():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if previous.get("status") == "complete":
            report.update({
                "status": "complete", "stage": "already_uploaded",
                "completed_at": now(), "previous_state": previous,
                "worker_inventory": previous.get("worker_inventory") or [],
                "planned_merge_chapters": previous.get("merged_chapters") or [],
                "merged_chapters": previous.get("merged_chapters") or [],
                "video_id": previous.get("video_id"), "video_url": previous.get("video_url"),
            })
            write_report(report_path, report)
            print(json.dumps(previous, ensure_ascii=False), flush=True)
            return
    output = run_root / "merged-audiobook.mp4"; output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_name:
        temp = Path(temp_name)
        title, cover, end_chapter, config = source_metadata_from_github(artifacts, temp)
        report.update({"stage": "inventory", "book_title": title})
        write_report(report_path, report)
        def record_worker_progress(item):
            existing = [
                record for record in report["worker_inventory"]
                if record.get("worker_id") != item.get("worker_id")
            ]
            report["worker_inventory"] = existing + [item]
            write_report(report_path, report)

        worker_inventory = inventory_worker_artifacts(
            worker_artifacts, config, temp, on_progress=record_worker_progress
        )
        merged_chapters = [chapter for item in worker_inventory for chapter in item["mp4_chapters"]]
        report.update({
            "stage": "inventory_complete",
            "worker_inventory": [
                {key: value for key, value in item.items() if key != "artifact"}
                for item in worker_inventory
            ],
            "planned_merge_chapters": merged_chapters,
        })
        write_report(report_path, report)
        report["stage"] = "merging"
        write_report(report_path, report)
        merged_chapters = merge_artifacts_to_bucket(worker_inventory, output, temp)
        report.update({"stage": "merge_complete", "merged_chapters": merged_chapters})
        write_report(report_path, report)
        from src.metadata_gen import generate_video_description, generate_video_title
        from src.youtube_api_uploader import get_authenticated_service, upload_video_file
        youtube_title = generate_video_title(title, 1, end_chapter); description = generate_video_description(title, 1, end_chapter)
        report["stage"] = "uploading"
        write_report(report_path, report)
        video_id = upload_video_file(get_authenticated_service(), str(output), youtube_title, description, privacy_status=args.privacy, cover_path=str(cover))
        state = {"status": "complete", "source_run_id": args.run_id, "completed_at": now(), "video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}", "chapters": end_chapter, "merged_chapters": merged_chapters, "worker_inventory": [{key: value for key, value in item.items() if key != "artifact"} for item in worker_inventory]}
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        report.update({
            "status": "complete", "stage": "upload_complete", "completed_at": now(),
            "video_id": video_id, "video_url": state["video_url"],
        })
        write_report(report_path, report)
    output.unlink()
    print(json.dumps(state, ensure_ascii=False), flush=True)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preflight", "shard", "finalize", "summary", "legacy"), default="legacy")
    parser.add_argument("--repository")
    parser.add_argument("--run-id", type=normalize_run_id)
    parser.add_argument("--bucket-mount", type=Path)
    parser.add_argument("--expected-workers", type=int, default=0)
    parser.add_argument("--expected-shards", type=int, default=0)
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--worker-start", type=int, default=0)
    parser.add_argument("--worker-end", type=int, default=0)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="public")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.mode == "summary":
        if not args.report:
            raise RuntimeError("--report is required for summary mode")
        print_summary(args.report)
        return 0
    if not args.repository or not args.run_id or not args.bucket_mount:
        raise RuntimeError("--repository, --run-id and --bucket-mount are required")
    if args.mode != "preflight" and shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required")
    try:
        if args.mode == "preflight":
            run_preflight(args)
        elif args.mode == "shard":
            run_shard(args)
        elif args.mode == "finalize":
            run_finalize(args)
        else:
            finalize_direct(args)
        return 0
    except Exception as error:
        if args.mode in ("preflight", "shard", "finalize"):
            print(json.dumps({"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        report_path = args.bucket_mount / "runs" / args.run_id / "merge-report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {
                "source_run_id": args.run_id, "worker_inventory": [], "merged_chapters": []
            }
            report.update({
                "status": "failed", "failed_at": now(),
                "error_type": type(error).__name__, "error": str(error),
            })
            write_report(report_path, report)
        except Exception as report_error:
            print(f"Could not persist merge report: {report_error}", file=sys.stderr)
        print(json.dumps({"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}, ensure_ascii=False, indent=2), file=sys.stderr); return 1


if __name__ == "__main__": sys.exit(main())
