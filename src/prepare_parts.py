"""Plan the whole book, then merge locked Parts on up to 17 matrix workers."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil
from pathlib import Path
import yaml
try:
    from .artifact_validation import validate_srt, validate_video
    from .part_builder import merge_part_videos
    from .youtube_api_uploader import (build_part_plan_from_inventory, confirmed_missing_from_directory, download_artifact_task, generate_part_srt, get_run_artifact_names, scan_artifact_chapters, validate_chapter_inventory)
except ImportError:
    from artifact_validation import validate_srt, validate_video
    from part_builder import merge_part_videos
    from youtube_api_uploader import (build_part_plan_from_inventory, confirmed_missing_from_directory, download_artifact_task, generate_part_srt, get_run_artifact_names, scan_artifact_chapters, validate_chapter_inventory)

def plan_parts(run_id, repo, config_path, output_dir, work_dir, max_workers=17):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    selected = [int(n) for n in config.get("selected_indices") or []]
    if not selected: raise RuntimeError("shared config has no selected_indices")
    output, work = Path(output_dir), Path(work_dir); output.mkdir(parents=True, exist_ok=True)
    names = get_run_artifact_names(str(run_id), repo)
    if not names: raise RuntimeError(f"source Run {run_id} has no worker artifacts")
    inventory, source_missing = [], set()
    for name in names:
        expanded = work / name
        if not download_artifact_task(str(run_id), repo, name, str(expanded)): raise RuntimeError(f"could not download {name}")
        source_missing.update(confirmed_missing_from_directory(str(expanded)))
        scanned = scan_artifact_chapters(str(expanded), name)
        if not scanned and not source_missing: raise RuntimeError(f"{name} contains no chapter MP4")
        inventory.extend(scanned); shutil.rmtree(expanded, ignore_errors=True)
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
    by_chapter={int(x["chap_num"]):x for x in inventory}; completed=[]
    for part in assigned:
        number=int(part["part_num"]); start,end=int(part["start_chap"]),int(part["end_chap"])
        missing_chapters=[int(c) for c in part["chapters"] if int(c) not in by_chapter]
        if missing_chapters: raise RuntimeError(f"Part {number} is missing chapter media: {missing_chapters}")
        items=[by_chapter[int(c)] for c in part["chapters"]]
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
        completed_part={**part,"video":video.name,"subtitle":subtitle.name,"video_validation":validate_video(str(video),float(part["duration"])),"subtitle_validation":validate_srt(str(subtitle),float(part["duration"]))}
        completed.append(completed_part)
        hf_token=os.environ.get("HF_TOKEN",""); hf_repo=os.environ.get("HF_ARCHIVE_REPO","").strip()
        if not hf_token: raise RuntimeError("HF_TOKEN is required for every merge worker")
        from huggingface_hub import HfApi
        api=HfApi(token=hf_token)
        if not hf_repo: hf_repo=f"{api.whoami()['name']}/audiobook-archive"
        api.create_repo(hf_repo,repo_type="dataset",private=True,exist_ok=True)
        folder=f"有聲小說_{safe_hf_name(plan['book_title'])}_第{number:02d}部_第{start:04d}章-第{end:04d}章"
        remote_root=f"有聲小說/{safe_hf_name(plan['book_title'])}/{folder}"
        api.upload_file(path_or_fileobj=str(video),path_in_repo=f"{remote_root}/{video.name}",repo_id=hf_repo,repo_type="dataset",commit_message=f"Stage {plan['book_title']} Part {number:02d}")
        api.upload_file(path_or_fileobj=str(subtitle),path_in_repo=f"{remote_root}/{subtitle.name}",repo_id=hf_repo,repo_type="dataset",commit_message=f"Stage {plan['book_title']} Part {number:02d} subtitle")
        merge_manifest={"schema_version":1,"status":"merge_complete","source_run_id":plan["source_run_id"],"book_title":plan["book_title"],"part":completed_part,"files":{"video":{"path":f"{remote_root}/{video.name}","bytes":video.stat().st_size,"sha256":_sha256(video)},"subtitle":{"path":f"{remote_root}/{subtitle.name}","bytes":subtitle.stat().st_size,"sha256":_sha256(subtitle)}}}
        api.upload_file(path_or_fileobj=(json.dumps(merge_manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8"),path_in_repo=f"{remote_root}/merge_manifest.json",repo_id=hf_repo,repo_type="dataset",commit_message=f"Complete merge {plan['book_title']} Part {number:02d}")
        print(f"[HF_MEDIA_MARKER] DONE | Part {number} | Ch {start}~{end} | {remote_root}",flush=True)
    (output/"shard-manifest.json").write_text(json.dumps({"source_run_id":plan["source_run_id"],"parts":completed},ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); return completed

def _sha256(path):
    digest=hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda:handle.read(8*1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()

def fetch_parts_from_hf(plan_path, output_dir):
    from huggingface_hub import HfApi, hf_hub_download
    token=os.environ.get("HF_TOKEN",""); repo=os.environ.get("HF_ARCHIVE_REPO","").strip()
    if not token: raise RuntimeError("HF_TOKEN is required")
    api=HfApi(token=token)
    if not repo: repo=f"{api.whoami()['name']}/audiobook-archive"
    plan=json.loads(Path(plan_path).read_text(encoding="utf-8")); output=Path(output_dir); output.mkdir(parents=True,exist_ok=True)
    for part in sorted(plan["parts"],key=lambda p:int(p["part_num"])):
        number,start,end=int(part["part_num"]),int(part["start_chap"]),int(part["end_chap"])
        folder=f"有聲小說_{safe_hf_name(plan['book_title'])}_第{number:02d}部_第{start:04d}章-第{end:04d}章"
        root=f"有聲小說/{safe_hf_name(plan['book_title'])}/{folder}"
        manifest_path=hf_hub_download(repo,f"{root}/merge_manifest.json",repo_type="dataset",token=token)
        manifest=json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if manifest.get("status")!="merge_complete" or str(manifest.get("source_run_id"))!=str(plan["source_run_id"]): raise RuntimeError(f"invalid HF merge manifest for Part {number}")
        for kind in ("video","subtitle"):
            info=manifest["files"][kind]; cached=hf_hub_download(repo,info["path"],repo_type="dataset",token=token)
            target=output/Path(info["path"]).name; shutil.copy2(cached,target)
            if target.stat().st_size!=int(info["bytes"]) or _sha256(target)!=info["sha256"]: raise RuntimeError(f"HF {kind} verification failed for Part {number}")
    config=Path(plan_path).with_name("config.yaml")
    if config.is_file(): shutil.copy2(config,output/"config.yaml")
    shutil.copy2(plan_path,output/"parts-plan.json")

def safe_hf_name(value):
    import re
    value=re.sub(r'[<>:"/\\|?*\x00-\x1f]',"_",str(value or "").strip())
    return re.sub(r"\s+"," ",value).strip(" ._") or "未命名"

def main():
    p=argparse.ArgumentParser(); p.add_argument("--mode",required=True,choices=["plan","merge","fetch"]); p.add_argument("--run-id"); p.add_argument("--repo",default=""); p.add_argument("--config"); p.add_argument("--plan"); p.add_argument("--part-numbers",default=""); p.add_argument("--output-dir",required=True); p.add_argument("--work-dir",default="temp_prepare_parts"); p.add_argument("--github-output"); a=p.parse_args()
    if a.mode=="plan":
        result=plan_parts(a.run_id,a.repo,a.config,a.output_dir,a.work_dir)
        if a.github_output:
            with open(a.github_output,"a",encoding="utf-8") as h: h.write("matrix="+json.dumps(result["matrix"],separators=(",",":"))+"\n"+f"part_count={len(result['parts'])}\n")
    elif a.mode=="merge": merge_assigned_parts(a.plan,[int(n) for n in a.part_numbers.split(",") if n],a.repo,a.output_dir,a.work_dir)
    else: fetch_parts_from_hf(a.plan,a.output_dir)
if __name__=="__main__": main()
