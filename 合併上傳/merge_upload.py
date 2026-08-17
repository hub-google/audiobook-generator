"""Checkpointed Actions-artifact merge and single-video YouTube upload."""
from __future__ import annotations

import argparse, json, os, re, shutil, subprocess, sys, tempfile, traceback, zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests

# GitHub Actions turns carriage-return progress redraws into thousands of log
# lines.  Keep third-party transfer bars disabled and emit one meaningful line
# per pipeline operation below instead.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
try:
    from huggingface_hub import HfApi, hf_hub_download
except ImportError:  # Allows ordering/unit tests before optional CI deps are installed.
    HfApi = None
    hf_hub_download = None

CHAPTER_RE = re.compile(r"chapter_(\d+)", re.I)
WORKER_RE = re.compile(r"mp4-worker-(\d+)$", re.I)
RUN_URL_RE = re.compile(r"^https?://github\.com/[^/]+/[^/]+/actions/runs/(\d+)/?(?:[?#].*)?$", re.I)

def normalize_run_id(value):
    value = str(value).strip()
    if value.isdigit():
        return value
    match = RUN_URL_RE.fullmatch(value)
    if match:
        return match.group(1)
    raise argparse.ArgumentTypeError(
        "run ID must be digits or a GitHub Actions URL ending in /actions/runs/<ID>"
    )

def normalize_book_title(value):
    title = str(value).strip().strip("《》")
    title = re.sub(r"\s*[\(（]\s*已完結\s*[\)）]\s*$", "", title)
    if not title:
        raise argparse.ArgumentTypeError("book title cannot be empty")
    return title

def chapter_number(path):
    match = CHAPTER_RE.search(Path(path).name)
    if not match: raise ValueError(f"Cannot determine chapter number from {Path(path).name!r}")
    return int(match.group(1))

def ordered_chapter_videos(root):
    videos = [p for p in Path(root).rglob("*.mp4") if CHAPTER_RE.search(p.name)]
    if not videos: raise RuntimeError(f"No chapter MP4 files found below {root}")
    return sorted(videos, key=lambda p: (chapter_number(p), str(p)))

def merge(videos, output, concat_file):
    quote = lambda p: str(Path(p).resolve()).replace("'", "'\\''")
    Path(concat_file).write_text("ffconcat version 1.0\n" + "".join(f"file '{quote(p)}'\n" for p in videos), encoding="utf-8")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", str(output)], check=True)

def now(): return datetime.now(timezone.utc).isoformat()

def worker_sort_key(name):
    match = WORKER_RE.fullmatch(str(name))
    return int(match.group(1)) if match else sys.maxsize

class Pipeline:
    def __init__(self, args):
        if HfApi is None:
            raise RuntimeError("huggingface_hub is required for persistent checkpoints")
        self.args, self.work = args, args.work_dir.resolve(); self.work.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work / "state.json"
        self.hf = HfApi(token=os.environ["HF_TOKEN"])
        self.repo_id = args.checkpoint_repo or f"{self.hf.whoami()['name']}/audiobook-merge-checkpoints"
        self.prefix, self.remote_files = f"runs/{args.run_id}", set()
        self.state_remote = f"{self.prefix}/state.json"
        self.state = {"source_run_id": str(args.run_id), "status": "running", "current_stage": "initializing", "completed_workers": [], "started_at": now(), "checkpoint_repo": self.repo_id}

    def save(self, **updates):
        self.state.update(updates); self.state["updated_at"] = now()
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        try: self.hf.upload_file(path_or_fileobj=str(self.state_path), path_in_repo=self.state_remote, repo_id=self.repo_id, repo_type="dataset", commit_message=f"Run {self.args.run_id}: {self.state['current_stage']}")
        except Exception as error: print(f"Warning: state checkpoint upload failed: {error}", flush=True)

    def prepare(self):
        self.hf.create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
        self.remote_files = set(self.hf.list_repo_files(self.repo_id, repo_type="dataset"))
        remote_state = self.state_remote
        if remote_state in self.remote_files:
            cached = hf_hub_download(self.repo_id, remote_state, repo_type="dataset", token=os.environ["HF_TOKEN"])
            self.state.update(json.loads(Path(cached).read_text(encoding="utf-8")))
        self.save(status="running", current_stage="discover_artifacts", error=None)

    def prepare_worker(self, worker_name):
        """Prepare isolated state for one matrix worker without state-file races."""
        self.hf.create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
        self.remote_files = set(self.hf.list_repo_files(self.repo_id, repo_type="dataset"))
        self.state_remote = f"{self.prefix}/worker-state/{worker_name}.json"
        if self.state_remote in self.remote_files:
            cached = hf_hub_download(self.repo_id, self.state_remote, repo_type="dataset", token=os.environ["HF_TOKEN"])
            self.state.update(json.loads(Path(cached).read_text(encoding="utf-8")))
        self.save(status="running", current_stage="discover_worker_artifact", current_worker=worker_name, error=None)

    @staticmethod
    def gh_headers(): return {"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def artifacts(self):
        result = [a for a in self.all_artifacts() if WORKER_RE.fullmatch(a["name"])]
        result.sort(key=lambda a: int(WORKER_RE.fullmatch(a["name"]).group(1)))
        if not result: raise RuntimeError("No non-expired mp4-worker-* artifacts found in the source run")
        return result

    def all_artifacts(self):
        artifacts, page = [], 1
        while True:
            url = f"https://api.github.com/repos/{self.args.repository}/actions/runs/{self.args.run_id}/artifacts?per_page=100&page={page}"
            response = requests.get(url, headers=self.gh_headers(), timeout=60); response.raise_for_status()
            batch = response.json().get("artifacts", [])
            artifacts.extend(a for a in batch if not a.get("expired"))
            if len(batch) < 100: return artifacts
            page += 1

    def download_artifact(self, artifact, destination):
        destination = Path(destination); destination.mkdir(parents=True, exist_ok=True)
        archive = destination / "artifact.zip"
        with requests.get(artifact["archive_download_url"], headers=self.gh_headers(), stream=True, timeout=(30, 300)) as response:
            response.raise_for_status()
            with archive.open("wb") as handle:
                for block in response.iter_content(1024 * 1024):
                    if block: handle.write(block)
        with zipfile.ZipFile(archive) as source: source.extractall(destination)
        archive.unlink()

    def source_metadata(self):
        """Read the source run's own name and cover; never generate replacements."""
        candidates = self.all_artifacts()
        config_artifact = next((a for a in candidates if a["name"] == "shared-config"), None)
        cover_artifact = next((a for a in candidates if a["name"] == "source-book-metadata"), None)
        if not config_artifact:
            raise RuntimeError("Source run has no shared-config artifact; cannot determine its original book name")

        metadata_dir = self.work / "source-metadata"
        self.download_artifact(config_artifact, metadata_dir / "config")
        config_path = next((p for p in (metadata_dir / "config").rglob("config.yaml")), None)
        if not config_path:
            raise RuntimeError("shared-config artifact does not contain config.yaml")
        import yaml
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        title = normalize_book_title(config.get("book_title", ""))

        cover = self.work / "metadata" / "youtube_cover.jpg"
        cover.parent.mkdir(parents=True, exist_ok=True)
        if cover_artifact:
            self.download_artifact(cover_artifact, metadata_dir / "cover")
            covers = sorted((metadata_dir / "cover").rglob("youtube_cover.jpg"), key=lambda p: str(p).lower())
            if not covers:
                raise RuntimeError("source-book-metadata contains no original youtube_cover.jpg; refusing to generate a replacement")
            shutil.copy2(covers[0], cover)
            source = {"source_cover_artifact": cover_artifact["name"], "source_cover_file": str(covers[0])}
        else:
            # Runs created before source-book-metadata existed can still reuse
            # the exact thumbnail already uploaded on the owner's YouTube
            # channel.  This downloads pixels from YouTube; it does not invoke
            # any image generator or compose a replacement.
            self.download_existing_youtube_cover(title, cover)
            source = {"source_cover_artifact": None, "source_cover_file": "owner YouTube video thumbnail"}
        self.save(source_book_title=title, **source)
        return title, cover

    @staticmethod
    def download_existing_youtube_cover(book_title, destination):
        from src.youtube_api_uploader import get_authenticated_service
        youtube = get_authenticated_service()
        response = youtube.search().list(
            part="snippet", forMine=True, type="video", q=f"《{book_title}》", maxResults=50
        ).execute()
        exact = [
            item for item in response.get("items", [])
            if f"《{book_title}》" in item.get("snippet", {}).get("title", "")
        ]
        if not exact:
            raise RuntimeError(
                "Legacy source run has no preserved cover artifact and no exact-title video "
                "was found on the authenticated YouTube channel; refusing to generate a replacement"
            )
        thumbnails = exact[0]["snippet"].get("thumbnails", {})
        for quality in ("maxres", "standard", "high", "medium", "default"):
            url = thumbnails.get(quality, {}).get("url")
            if not url: continue
            response = requests.get(url, timeout=60); response.raise_for_status()
            Path(destination).write_bytes(response.content)
            return
        raise RuntimeError("Exact-title YouTube video has no downloadable thumbnail")

    def stage_workers(self, artifacts):
        manifests = []
        for index, artifact in enumerate(artifacts, 1):
            name = artifact["name"]; chunk_remote = f"{self.prefix}/workers/{name}.mp4"; manifest_remote = f"{self.prefix}/workers/{name}.json"
            self.save(current_stage="stage_worker", current_worker=name, worker_progress=f"{index}/{len(artifacts)}")
            if chunk_remote in self.remote_files and manifest_remote in self.remote_files:
                print(f"[{index}/{len(artifacts)}] {name}: loading existing checkpoint manifest (source download and merge skipped).", flush=True)
                cached = hf_hub_download(self.repo_id, manifest_remote, repo_type="dataset", token=os.environ["HF_TOKEN"])
                manifests.append(json.loads(Path(cached).read_text(encoding="utf-8")))
                completed = set(self.state.get("completed_workers", [])) | {name}
                self.save(completed_workers=sorted(completed, key=worker_sort_key))
                continue
            print(f"[{index}/{len(artifacts)}] {name}: downloading source GitHub artifact.", flush=True)
            with tempfile.TemporaryDirectory(dir=self.work) as temp_name:
                temp = Path(temp_name); archive = temp / f"{name}.zip"
                with requests.get(artifact["archive_download_url"], headers=self.gh_headers(), stream=True, timeout=(30, 300)) as response:
                    response.raise_for_status()
                    with archive.open("wb") as handle:
                        for block in response.iter_content(8 * 1024 * 1024):
                            if block: handle.write(block)
                extracted = temp / "files"
                with zipfile.ZipFile(archive) as source: source.extractall(extracted)
                videos = ordered_chapter_videos(extracted); chunk = temp / f"{name}.mp4"
                print(f"[{index}/{len(artifacts)}] {name}: merging chapters {chapter_number(videos[0])}-{chapter_number(videos[-1])}.", flush=True)
                merge(videos, chunk, temp / "chapters.ffconcat")
                manifest = {"worker": name, "first_chapter": chapter_number(videos[0]), "last_chapter": chapter_number(videos[-1]), "chapters": [chapter_number(p) for p in videos], "remote_file": chunk_remote}
                manifest_path = temp / f"{name}.json"; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[{index}/{len(artifacts)}] {name}: uploading merged worker checkpoint.", flush=True)
                self.hf.upload_file(path_or_fileobj=str(chunk), path_in_repo=chunk_remote, repo_id=self.repo_id, repo_type="dataset")
                self.hf.upload_file(path_or_fileobj=str(manifest_path), path_in_repo=manifest_remote, repo_id=self.repo_id, repo_type="dataset")
                self.remote_files.update((chunk_remote, manifest_remote)); manifests.append(manifest)
                completed = set(self.state.get("completed_workers", [])) | {name}
                self.save(completed_workers=sorted(completed, key=worker_sort_key))
                print(f"[{index}/{len(artifacts)}] {name}: worker checkpoint complete.", flush=True)
        return sorted(manifests, key=lambda item: item["first_chapter"])

    def build_final(self, manifests):
        remote, output = f"{self.prefix}/merged/merged-audiobook.mp4", self.work / "merged-audiobook.mp4"
        self.save(current_stage="merge_final", current_worker=None)
        if remote in self.remote_files:
            print("Final merge: existing merged checkpoint found; merge skipped.", flush=True)
            self.save(final_merge_complete=True, merged_remote_file=remote)
            return Path(hf_hub_download(self.repo_id, remote, repo_type="dataset", token=os.environ["HF_TOKEN"]))
        chunks = self.work / "chunks"; chunks.mkdir(exist_ok=True); local = []
        for position, item in enumerate(manifests, 1):
            worker = item["worker"]
            print(f"[{position}/{len(manifests)}] {worker}: downloading worker checkpoint for final merge.", flush=True)
            local.append(Path(hf_hub_download(self.repo_id, item["remote_file"], repo_type="dataset", token=os.environ["HF_TOKEN"], local_dir=chunks)))
            downloaded = set(self.state.get("downloaded_worker_chunks", [])) | {worker}
            self.save(current_stage="download_worker_chunks", chunk_progress=f"{position}/{len(manifests)}", downloaded_worker_chunks=sorted(downloaded, key=worker_sort_key))
            print(f"[{position}/{len(manifests)}] {worker}: worker checkpoint download complete.", flush=True)
        print(f"Final merge: combining {len(local)} worker checkpoints in chapter order.", flush=True)
        merge(local, output, self.work / "workers.ffconcat")
        self.save(current_stage="save_merged_checkpoint")
        print("Final merge: uploading merged audiobook checkpoint.", flush=True)
        self.hf.upload_file(path_or_fileobj=str(output), path_in_repo=remote, repo_id=self.repo_id, repo_type="dataset"); self.remote_files.add(remote)
        self.save(merged_remote_file=remote, final_merge_complete=True)
        print("Final merge: complete and checkpoint saved.", flush=True)
        return output

    def metadata(self, manifests):
        from src.metadata_gen import generate_video_description, generate_video_title
        chapters = [chapter for item in manifests for chapter in item["chapters"]]
        self.save(current_stage="reuse_source_metadata_cover")
        title, cover = self.source_metadata()
        return {
            "title": generate_video_title(title, min(chapters), max(chapters)),
            "description": generate_video_description(title, min(chapters), max(chapters)),
            "cover_file": str(cover),
        }

    def upload(self, output, manifests):
        metadata = self.metadata(manifests)
        self.save(current_stage="youtube_upload", youtube_title=metadata["title"], cover_file=metadata["cover_file"])
        from src.youtube_api_uploader import get_authenticated_service, upload_video_file
        video_id = upload_video_file(
            get_authenticated_service(), str(output), metadata["title"], metadata["description"],
            privacy_status=self.args.privacy, cover_path=metadata["cover_file"],
        )
        self.save(current_stage="complete", status="complete", video_id=video_id, video_url=f"https://www.youtube.com/watch?v={video_id}", chapters=sum(len(x["chapters"]) for x in manifests), error=None, completed_at=now())

    def cleanup_large_checkpoints(self):
        """Delete run MP4s only after YouTube success; retain small JSON audit data."""
        large_files = sorted(
            path for path in self.remote_files
            if path.startswith(f"{self.prefix}/") and path.lower().endswith(".mp4")
        )
        self.save(current_stage="cleanup_hf_video_checkpoints", cleanup_status="running", cleanup_files=large_files)
        failures = []
        deleted_count = 0
        for position, remote_path in enumerate(large_files, 1):
            print(f"[{position}/{len(large_files)}] HF cleanup: deleting {remote_path}.", flush=True)
            try:
                self.hf.delete_file(
                    path_in_repo=remote_path, repo_id=self.repo_id,
                    repo_type="dataset", commit_message=f"Run {self.args.run_id}: remove uploaded video checkpoint",
                )
                self.remote_files.discard(remote_path)
                deleted_count += 1
            except Exception as error:
                failures.append({"file": remote_path, "error": str(error)})
        reclaimed_lfs_count = 0
        if not failures and large_files:
            try:
                # Removing Git pointers alone does not release LFS quota. Purge
                # only LFS objects belonging to this source-run prefix and
                # rewrite their history so the storage can actually be reclaimed.
                run_lfs_files = [
                    item for item in self.hf.list_lfs_files(self.repo_id, repo_type="dataset")
                    if item.filename.startswith(f"{self.prefix}/") and item.filename.lower().endswith(".mp4")
                ]
                if run_lfs_files:
                    self.hf.permanently_delete_lfs_files(
                        repo_id=self.repo_id, lfs_files=run_lfs_files,
                        rewrite_history=True, repo_type="dataset",
                    )
                    reclaimed_lfs_count = len(run_lfs_files)
            except Exception as error:
                failures.append({"file": "HF LFS history", "error": str(error)})
        cleanup_status = "complete" if not failures else "partial_failure"
        self.save(
            current_stage="complete", cleanup_status=cleanup_status,
            cleanup_deleted_count=deleted_count, cleanup_reclaimed_lfs_count=reclaimed_lfs_count,
            cleanup_failures=failures,
        )
        if failures:
            print(f"Warning: HF cleanup left {len(failures)} large checkpoint(s); see state.json.", flush=True)
        else:
            print(f"HF cleanup: removed {len(large_files)} large checkpoint(s); JSON audit files retained.", flush=True)

    def run(self):
        if self.args.worker_name:
            self.prepare_worker(self.args.worker_name)
            artifacts = [item for item in self.artifacts() if item["name"] == self.args.worker_name]
            if not artifacts:
                raise RuntimeError(f"Worker artifact {self.args.worker_name!r} was not found or has expired")
            self.save(artifact_count=1)
            self.stage_workers(artifacts)
            self.save(status="complete", current_stage="worker_complete", completed_at=now())
            return
        self.prepare(); artifacts = self.artifacts(); self.save(artifact_count=len(artifacts))
        manifests = self.stage_workers(artifacts)
        self.upload(self.build_final(manifests), manifests)
        self.cleanup_large_checkpoints()

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--repository", required=True); p.add_argument("--run-id", required=True, type=normalize_run_id)
    p.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private")
    p.add_argument("--checkpoint-repo", default=""); p.add_argument("--work-dir", default=Path("merge-upload-state"), type=Path)
    p.add_argument("--worker-name", default="", help="stage only this mp4-worker-* artifact (matrix mode)")
    return p.parse_args()

def main():
    args = parse_args()
    if shutil.which("ffmpeg") is None: raise RuntimeError("ffmpeg is required")
    pipeline = Pipeline(args)
    try: pipeline.run(); return 0
    except Exception as error:
        failure = {"stage": pipeline.state.get("current_stage", "initializing"), "type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc(), "failed_at": now()}
        pipeline.save(status="failed", error=failure); print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr); return 1

if __name__ == "__main__": sys.exit(main())
