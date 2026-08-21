"""Plan the whole book, then merge locked Parts on up to 17 matrix workers."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, subprocess
from pathlib import Path
import yaml
try:
    from .artifact_validation import validate_srt, validate_video
    from .part_builder import merge_part_videos
    from .youtube_api_uploader import (build_part_plan_from_inventory, confirmed_missing_from_directory, download_artifact_task, generate_part_srt, get_run_artifact_names, get_run_manifest_artifact_names, scan_artifact_chapters, validate_chapter_inventory)
except ImportError:
    from artifact_validation import validate_srt, validate_video
    from part_builder import merge_part_videos
    from youtube_api_uploader import (build_part_plan_from_inventory, confirmed_missing_from_directory, download_artifact_task, generate_part_srt, get_run_artifact_names, get_run_manifest_artifact_names, scan_artifact_chapters, validate_chapter_inventory)

def plan_parts(run_id, repo, config_path, output_dir, work_dir, max_workers=17):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    selected = [int(n) for n in config.get("selected_indices") or []]
    if not selected: raise RuntimeError("shared config has no selected_indices")
    output, work = Path(output_dir), Path(work_dir); output.mkdir(parents=True, exist_ok=True)
    manifest_names = get_run_manifest_artifact_names(str(run_id), repo) if repo else []
    names = get_run_artifact_names(str(run_id), repo)
    if not manifest_names and not names: raise RuntimeError(f"source Run {run_id} has no worker artifacts")
    inventory, source_missing = [], set()

    # Fast path: load lightweight manifest JSONs (only a few KB per worker)
    if manifest_names and (not names or len(manifest_names) >= len(names)):
        for name in manifest_names:
            expanded = work / name
            if not download_artifact_task(str(run_id), repo, name, str(expanded)):
                raise RuntimeError(f"could not download {name}")
            manifest_files = list(expanded.glob("**/*.json"))
            if not manifest_files:
                raise RuntimeError(f"{name} contains no manifest JSON")
            data = json.loads(manifest_files[0].read_text(encoding="utf-8"))
            worker_id = data.get("worker_id")
            for chapter in data.get("chapters", []):
                inventory.append({
                    "artifact": chapter.get("artifact") or f"mp4-worker-{worker_id}",
                    "chap_num": int(chapter["chap_num"]),
                    "dur": float(chapter["dur"]),
                })
            source_missing.update(int(c) for c in data.get("source_missing", []))
            shutil.rmtree(expanded, ignore_errors=True)
    else:
        # Fallback / backward compatibility: download worker artifacts
        for name in names:
            expanded = work / name
            if not download_artifact_task(str(run_id), repo, name, str(expanded)):
                raise RuntimeError(f"could not download {name}")
            manifest_files = list(expanded.glob("**/manifest-worker-*.json"))
            if manifest_files:
                data = json.loads(manifest_files[0].read_text(encoding="utf-8"))
                for chapter in data.get("chapters", []):
                    inventory.append({
                        "artifact": chapter.get("artifact") or name,
                        "chap_num": int(chapter["chap_num"]),
                        "dur": float(chapter["dur"]),
                    })
                source_missing.update(int(c) for c in data.get("source_missing", []))
            else:
                source_missing.update(confirmed_missing_from_directory(str(expanded)))
                scanned = scan_artifact_chapters(str(expanded), name)
                if not scanned and not source_missing:
                    raise RuntimeError(f"{name} contains no chapter MP4")
                inventory.extend(scanned)
            shutil.rmtree(expanded, ignore_errors=True)

    inventory.sort(key=lambda x: int(x["chap_num"]))
    validate_chapter_inventory(inventory, selected[0], selected[-1], source_missing)
    parts = build_part_plan_from_inventory(inventory, 10*3600, 11*3600, source_missing)
    if not parts: raise RuntimeError("validated chapter inventory produced no Parts")
    worker_count = min(int(max_workers), len(parts)); assignments = [[] for _ in range(worker_count)]
    for index, part in enumerate(parts): assignments[index % worker_count].append(int(part["part_num"]))
    matrix = {"include": [{"merge_worker_id": i, "part_numbers": ",".join(map(str, nums))} for i, nums in enumerate(assignments)]}
    manifest = {"schema_version": 1, "source_run_id": str(run_id), "book_title": str(config.get("book_title") or "有聲小說全集"), "selected_indices": selected, "source_missing_chapters": sorted(source_missing), "chapter_artifacts": {str(int(x["chap_num"])): x["artifact"] for x in inventory}, "parts": parts, "matrix": matrix}
    (output/"parts-plan.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    shutil.copy2(config_path, output/"config.yaml"); return manifest

def merge_assigned_parts(plan_path, part_numbers, repo, output_dir, work_dir):
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8")); wanted = {int(n) for n in part_numbers}
    assigned = [p for p in plan["parts"] if int(p["part_num"]) in wanted]
    if {int(p["part_num"]) for p in assigned} != wanted: raise RuntimeError("matrix assignment contains an unknown Part")
    artifacts = sorted({plan["chapter_artifacts"][str(c)] for p in assigned for c in p["chapters"]})
    output, work = Path(output_dir), Path(work_dir); output.mkdir(parents=True, exist_ok=True); inventory=[]
    for name in artifacts:
        expanded=work/name
        if not download_artifact_task(plan["source_run_id"], repo, name, str(expanded)): raise RuntimeError(f"could not download {name}")
        inventory.extend(scan_artifact_chapters(str(expanded), name))
    by_chapter={int(x["chap_num"]):x for x in inventory}; completed=[]; hf_operations=[]
    from huggingface_hub import CommitOperationAdd, HfApi
    for part in assigned:
        number=int(part["part_num"]); start,end=int(part["start_chap"]),int(part["end_chap"])
        missing_chapters=[int(c) for c in part["chapters"] if int(c) not in by_chapter]
        if missing_chapters: raise RuntimeError(f"Part {number} is missing chapter media: {missing_chapters}")
        items=[by_chapter[int(c)] for c in part["chapters"]]
        for x in items:
            if float(x.get("dur") or 0) <= 0:
                srt_p = x.get("srt_path")
                if srt_p and Path(srt_p).is_file():
                    try:
                        srt_val = validate_srt(srt_p)
                        if srt_val.get("end_seconds", 0) > 0:
                            x["dur"] = float(srt_val["end_seconds"])
                    except Exception:
                        pass
        missing_videos=[int(x["chap_num"]) for x in items if not x.get("path") or not Path(x["path"]).is_file()]
        missing_subtitles=[int(x["chap_num"]) for x in items if not x.get("srt_path") or not Path(x["srt_path"]).is_file() or Path(x["srt_path"]).stat().st_size == 0]
        invalid_durations=[int(x["chap_num"]) for x in items if float(x.get("dur") or 0) <= 0]
        if missing_videos or missing_subtitles or invalid_durations:
            raise RuntimeError(
                f"Part {number} artifact validation failed: missing_videos={missing_videos}, "
                f"missing_subtitles={missing_subtitles}, invalid_durations={invalid_durations}"
            )
        stem=f"{plan['book_title']}_Part_{number:02d}_Ch{start:04d}_to_Ch{end:04d}"; video,subtitle=output/f"{stem}.mp4",output/f"{stem}.srt"
        if not generate_part_srt(items,str(subtitle)): raise RuntimeError(f"could not generate Part {number} subtitle")
        if not merge_part_videos(dict(part,files=[x["path"] for x in items]),str(video)): raise RuntimeError(f"could not merge Part {number}")
        folder=f"有聲小說_{safe_hf_name(plan['book_title'])}_第{number:02d}部_第{start:04d}章-第{end:04d}章"
        remote_root=f"有聲小說/{safe_hf_name(plan['book_title'])}/{folder}"
        remote_video=f"{remote_root}/{video.name}"; remote_subtitle=f"{remote_root}/{subtitle.name}"
        completed_part={**part,"video":video.name,"subtitle":subtitle.name,"hf_video_path":remote_video,"hf_subtitle_path":remote_subtitle,"video_bytes":video.stat().st_size,"video_sha256":_sha256(video),"subtitle_bytes":subtitle.stat().st_size,"subtitle_sha256":_sha256(subtitle),"video_validation":validate_video(str(video),float(part["duration"])),"subtitle_validation":validate_srt(str(subtitle),float(part["duration"]))}
        completed.append(completed_part)
        hf_operations.append(CommitOperationAdd(path_in_repo=remote_video,path_or_fileobj=str(video)))
        hf_operations.append(CommitOperationAdd(path_in_repo=remote_subtitle,path_or_fileobj=str(subtitle)))
        files={"video":{"path":remote_video,"bytes":video.stat().st_size,"sha256":completed_part["video_sha256"]},"subtitle":{"path":remote_subtitle,"bytes":subtitle.stat().st_size,"sha256":completed_part["subtitle_sha256"]}}
        merge_manifest={"schema_version":1,"status":"merge_complete","source_run_id":plan["source_run_id"],"book_title":plan["book_title"],"part":completed_part,"files":files}
        part_manifest={"project":"有聲小說","book_title":plan["book_title"],"part_number":number,"start_chapter":start,"end_chapter":end,"chapters":[int(c) for c in part["chapters"]],"source_missing_chapters":[int(c) for c in plan.get("source_missing_chapters",[])],"source_run_id":str(plan["source_run_id"]),"queue_task_id":"","files":files,"status":"uploaded_pending_youtube_metadata"}
        sidecars={"merge_manifest.json":merge_manifest,"part_manifest.json":part_manifest,"media_info.json":_media_info(video)}
        for filename,payload in sidecars.items():
            local_sidecar=output/f"part-{number:02d}-{filename}"
            local_sidecar.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            hf_operations.append(CommitOperationAdd(path_in_repo=f"{remote_root}/{filename}",path_or_fileobj=str(local_sidecar)))
    if hf_operations:
        hf_token=os.environ.get("HF_TOKEN",""); hf_repo=os.environ.get("HF_ARCHIVE_REPO","").strip()
        if not hf_token: raise RuntimeError("HF_TOKEN is required for every merge worker")
        api=HfApi(token=hf_token)
        if not hf_repo: hf_repo=f"{api.whoami()['name']}/audiobook-archive"
        api.create_repo(hf_repo,repo_type="dataset",private=True,exist_ok=True)
        api.create_commit(repo_id=hf_repo,repo_type="dataset",operations=hf_operations,commit_message=f"Archive merged Parts for {plan['book_title']}: {','.join(map(str,sorted(wanted)))}")
    for item in completed:
        print(f"[HF_MEDIA_MARKER] DONE | Part {item['part_num']} | Ch {item['start_chap']}~{item['end_chap']} | {item['hf_video_path']}",flush=True)
        (output/item["video"]).unlink(missing_ok=True)
    shard_name="shard-manifest-"+"-".join(map(str,sorted(wanted)))+".json"
    (output/shard_name).write_text(json.dumps({"source_run_id":plan["source_run_id"],"parts":completed},ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); return completed

def _sha256(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda:handle.read(8*1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()

def _media_info(path):
    result=subprocess.run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(path)],capture_output=True,text=True,timeout=120,check=False)
    if result.returncode != 0: return {"probe_error":result.stderr[-2000:]}
    return json.loads(result.stdout)

def fetch_parts_from_hf(plan_path, output_dir, sidecar_dir=None):
    from huggingface_hub import HfApi, hf_hub_download
    token=os.environ.get("HF_TOKEN",""); repo=os.environ.get("HF_ARCHIVE_REPO","").strip()
    if not token: raise RuntimeError("HF_TOKEN is required")
    api=HfApi(token=token)
    if not repo: repo=f"{api.whoami()['name']}/audiobook-archive"
    plan=json.loads(Path(plan_path).read_text(encoding="utf-8")); output=Path(output_dir); output.mkdir(parents=True,exist_ok=True)
    sidecars=Path(sidecar_dir or output_dir)
    manifests={}
    for manifest_path in sidecars.glob("**/shard-manifest-*.json"):
        data=json.loads(manifest_path.read_text(encoding="utf-8"))
        if str(data.get("source_run_id"))!=str(plan["source_run_id"]): raise RuntimeError(f"wrong source Run in {manifest_path}")
        for item in data.get("parts",[]): manifests[int(item["part_num"])]=item
    for part in sorted(plan["parts"],key=lambda p:int(p["part_num"])):
        number,start,end=int(part["part_num"]),int(part["start_chap"]),int(part["end_chap"])
        info=manifests.get(number)
        if not info: raise RuntimeError(f"missing merge-worker sidecar for Part {number}")
        cached=hf_hub_download(repo,info["hf_video_path"],repo_type="dataset",token=token)
        target=output/info["video"]; shutil.copy2(cached,target)
        if target.stat().st_size!=int(info["video_bytes"]) or _sha256(target)!=info["video_sha256"]: raise RuntimeError(f"HF MP4 verification failed for Part {number}")
        subtitle_source=next(iter(sidecars.glob(f"**/{info['subtitle']}")),None)
        if not subtitle_source: raise RuntimeError(f"GitHub artifact subtitle is missing for Part {number}")
        shutil.copy2(subtitle_source,output/info["subtitle"])
    config=Path(plan_path).with_name("config.yaml")
    if config.is_file(): shutil.copy2(config,output/"config.yaml")
    shutil.copy2(plan_path,output/"parts-plan.json")

def safe_hf_name(value):
    import re
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]',"_",str(value or "").strip())
    return re.sub(r"\s+"," ",value).strip(" ._") or "未命名"

def main():
    p=argparse.ArgumentParser(); p.add_argument("--mode",required=True,choices=["plan","merge","fetch"]); p.add_argument("--run-id"); p.add_argument("--repo",default=""); p.add_argument("--config"); p.add_argument("--plan"); p.add_argument("--part-numbers",default=""); p.add_argument("--output-dir",required=True); p.add_argument("--sidecar-dir"); p.add_argument("--work-dir",default="temp_prepare_parts"); p.add_argument("--github-output"); a=p.parse_args()
    if a.mode=="plan":
        result=plan_parts(a.run_id,a.repo,a.config,a.output_dir,a.work_dir)
        if a.github_output:
            with open(a.github_output,"a",encoding="utf-8") as h: h.write("matrix="+json.dumps(result["matrix"],separators=(",",":"))+"\n"+f"part_count={len(result['parts'])}\n")
    elif a.mode=="merge": merge_assigned_parts(a.plan,[int(n) for n in a.part_numbers.split(",") if n],a.repo,a.output_dir,a.work_dir)
    else: fetch_parts_from_hf(a.plan,a.output_dir,a.sidecar_dir)
if __name__=="__main__": main()
