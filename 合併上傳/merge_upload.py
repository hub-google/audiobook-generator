"""Checkpointed Actions-artifact merge and single-video YouTube upload."""
from __future__ import annotations

import argparse, json, os, re, shutil, subprocess, sys, tempfile, traceback, zipfile
from datetime import datetime, timezone
from pathlib import Path
import requests
try:
    from huggingface_hub import HfApi, hf_hub_download
except ImportError:  # Allows ordering/unit tests before optional CI deps are installed.
    HfApi = None
    hf_hub_download = None

CHAPTER_RE = re.compile(r"chapter_(\d+)", re.I)
WORKER_RE = re.compile(r"mp4-worker-(\d+)$", re.I)

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

class Pipeline:
    def __init__(self, args):
        if HfApi is None:
            raise RuntimeError("huggingface_hub is required for persistent checkpoints")
        self.args, self.work = args, args.work_dir.resolve(); self.work.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work / "state.json"
        self.hf = HfApi(token=os.environ["HF_TOKEN"])
        self.repo_id = args.checkpoint_repo or f"{self.hf.whoami()['name']}/audiobook-merge-checkpoints"
        self.prefix, self.remote_files = f"runs/{args.run_id}", set()
        self.state = {"source_run_id": str(args.run_id), "status": "running", "current_stage": "initializing", "completed_workers": [], "started_at": now(), "checkpoint_repo": self.repo_id}

    def save(self, **updates):
        self.state.update(updates); self.state["updated_at"] = now()
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        try: self.hf.upload_file(str(self.state_path), f"{self.prefix}/state.json", self.repo_id, repo_type="dataset", commit_message=f"Run {self.args.run_id}: {self.state['current_stage']}")
        except Exception as error: print(f"Warning: state checkpoint upload failed: {error}", flush=True)

    def prepare(self):
        self.hf.create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
        self.remote_files = set(self.hf.list_repo_files(self.repo_id, repo_type="dataset"))
        remote_state = f"{self.prefix}/state.json"
        if remote_state in self.remote_files:
            cached = hf_hub_download(self.repo_id, remote_state, repo_type="dataset", token=os.environ["HF_TOKEN"])
            self.state.update(json.loads(Path(cached).read_text(encoding="utf-8")))
        self.save(status="running", current_stage="discover_artifacts", error=None)

    @staticmethod
    def gh_headers(): return {"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def artifacts(self):
        url = f"https://api.github.com/repos/{self.args.repository}/actions/runs/{self.args.run_id}/artifacts?per_page=100"
        response = requests.get(url, headers=self.gh_headers(), timeout=60); response.raise_for_status()
        result = [a for a in response.json().get("artifacts", []) if WORKER_RE.fullmatch(a["name"]) and not a.get("expired")]
        result.sort(key=lambda a: int(WORKER_RE.fullmatch(a["name"]).group(1)))
        if not result: raise RuntimeError("No non-expired mp4-worker-* artifacts found in the source run")
        return result

    def stage_workers(self, artifacts):
        manifests = []
        for index, artifact in enumerate(artifacts, 1):
            name = artifact["name"]; chunk_remote = f"{self.prefix}/workers/{name}.mp4"; manifest_remote = f"{self.prefix}/workers/{name}.json"
            self.save(current_stage="stage_worker", current_worker=name, worker_progress=f"{index}/{len(artifacts)}")
            if chunk_remote in self.remote_files and manifest_remote in self.remote_files:
                cached = hf_hub_download(self.repo_id, manifest_remote, repo_type="dataset", token=os.environ["HF_TOKEN"])
                manifests.append(json.loads(Path(cached).read_text(encoding="utf-8")))
                print(f"Checkpoint hit: {name}; source artifact download skipped.", flush=True); continue
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
                merge(videos, chunk, temp / "chapters.ffconcat")
                manifest = {"worker": name, "first_chapter": chapter_number(videos[0]), "last_chapter": chapter_number(videos[-1]), "chapters": [chapter_number(p) for p in videos], "remote_file": chunk_remote}
                manifest_path = temp / f"{name}.json"; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                self.hf.upload_file(str(chunk), chunk_remote, self.repo_id, repo_type="dataset")
                self.hf.upload_file(str(manifest_path), manifest_remote, self.repo_id, repo_type="dataset")
                self.remote_files.update((chunk_remote, manifest_remote)); manifests.append(manifest)
                self.save(completed_workers=sorted(set(self.state.get("completed_workers", [])) | {name}))
        return sorted(manifests, key=lambda item: item["first_chapter"])

    def build_final(self, manifests):
        remote, output = f"{self.prefix}/merged/merged-audiobook.mp4", self.work / "merged-audiobook.mp4"
        self.save(current_stage="merge_final", current_worker=None)
        if remote in self.remote_files:
            print("Checkpoint hit: final merged video; merge skipped.", flush=True)
            return Path(hf_hub_download(self.repo_id, remote, repo_type="dataset", token=os.environ["HF_TOKEN"]))
        chunks = self.work / "chunks"; chunks.mkdir(exist_ok=True); local = []
        for position, item in enumerate(manifests, 1):
            local.append(Path(hf_hub_download(self.repo_id, item["remote_file"], repo_type="dataset", token=os.environ["HF_TOKEN"], local_dir=chunks)))
            self.save(current_stage="download_worker_chunks", chunk_progress=f"{position}/{len(manifests)}")
        merge(local, output, self.work / "workers.ffconcat")
        self.save(current_stage="save_merged_checkpoint")
        self.hf.upload_file(str(output), remote, self.repo_id, repo_type="dataset"); self.remote_files.add(remote)
        self.save(merged_remote_file=remote); return output

    def upload(self, output, manifests):
        self.save(current_stage="youtube_upload")
        from src.youtube_api_uploader import get_authenticated_service, upload_video_file
        video_id = upload_video_file(get_authenticated_service(), str(output), self.args.title, self.args.description or f"Merged from GitHub Actions run {self.args.run_id}.", privacy_status=self.args.privacy)
        self.save(current_stage="complete", status="complete", video_id=video_id, video_url=f"https://www.youtube.com/watch?v={video_id}", chapters=sum(len(x["chapters"]) for x in manifests), error=None, completed_at=now())

    def run(self):
        self.prepare(); artifacts = self.artifacts(); self.save(artifact_count=len(artifacts))
        manifests = self.stage_workers(artifacts); self.upload(self.build_final(manifests), manifests)

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--repository", required=True); p.add_argument("--run-id", required=True); p.add_argument("--title", required=True)
    p.add_argument("--description", default=""); p.add_argument("--privacy", choices=("private", "unlisted", "public"), default="private")
    p.add_argument("--checkpoint-repo", default=""); p.add_argument("--work-dir", default=Path("merge-upload-state"), type=Path)
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
