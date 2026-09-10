"""GitHub Actions backend for HF merge plans and two-phase YouTube uploads."""
from __future__ import annotations

import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
from hf_catalog import HfCatalog
from merge_plan import build_plan
from src.artifact_validation import validate_video

CHUNK = 8 * 1024 * 1024
TRANSFER_RETRIES = 5
SOURCE_DOWNLOAD_RETRIES = 8
YOUTUBE_DESCRIPTION_LIMIT = 5000
YOUTUBE_TIMELINE_BUDGET = 4400
TIMELINE_GROUP_SIZES = (2, 5, 10, 20, 50, 100)


def _output_chapters(item):
    return [chapter for part in item["parts"] for chapter in part.get("chapter_timeline") or []]


def _timestamp(seconds):
    rounded = int(float(seconds) + 0.5)
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _chapter_starts(chapters):
    starts, elapsed = [], 0.0
    for chapter in chapters:
        starts.append(elapsed)
        elapsed += float(chapter.get("dur") or 0.0)
    return starts


def _chapter_number(chapter):
    return int(chapter.get("chap_num") or chapter.get("chapter") or 0)


def _grouped_chapter_timeline(chapters, group_size):
    starts = _chapter_starts(chapters)
    lines = ["⏳ 影片章節時間軸："]
    for offset in range(0, len(chapters), group_size):
        group = chapters[offset:offset + group_size]
        first, last = _chapter_number(group[0]), _chapter_number(group[-1])
        label = f"第{first}章" if first == last else f"第{first}–{last}章"
        lines.append(f"{_timestamp(starts[offset])} {label}")
    return "\n".join(lines)


def output_chapter_timeline(item):
    chapters = _output_chapters(item)
    if not chapters:
        raise RuntimeError("merge output has no chapter timeline")
    from src.youtube_upload.metadata import build_chapter_timeline
    timeline = build_chapter_timeline(chapters)
    if len(timeline) <= YOUTUBE_TIMELINE_BUDGET:
        return timeline
    # Keep every displayed timestamp first, while shortening only its label.
    compact = [timeline.splitlines()[0]]
    for line in timeline.splitlines()[1:]:
        timestamp, _, label = line.partition(" ")
        number = re.search(r"\d+", label)
        compact.append(f"{timestamp} 第{number.group(0)}章" if number else line)
    result = "\n".join(compact)
    if len(result) <= YOUTUBE_TIMELINE_BUDGET:
        return result
    for group_size in TIMELINE_GROUP_SIZES:
        result = _grouped_chapter_timeline(chapters, group_size)
        if len(result) <= YOUTUBE_TIMELINE_BUDGET:
            return result
    # Pathological chapter counts may need a coarser group than the normal
    # presets. Keep increasing it until a legal, non-empty description exists.
    group_size = TIMELINE_GROUP_SIZES[-1] * 2
    while group_size < len(chapters):
        result = _grouped_chapter_timeline(chapters, group_size)
        if len(result) <= YOUTUBE_TIMELINE_BUDGET:
            return result
        group_size *= 2
    result = _grouped_chapter_timeline(chapters, len(chapters))
    if not result or len(result) > YOUTUBE_TIMELINE_BUDGET:
        raise RuntimeError("cannot build a legal YouTube chapter description")
    return result

def repo_token():
    token=os.environ["HF_TOKEN"]; repo=os.environ.get("HF_ARCHIVE_REPO","").strip()
    if not repo: repo=f"{HfApi(token=token).whoami()['name']}/audiobook-archive"
    return repo,token
def api():
    repo, token = repo_token(); return HfApi(token=token), repo, token
def remote_json(path, revision="main"):
    _, repo, token = api(); return json.loads(Path(hf_hub_download(repo,path,repo_type="dataset",token=token,revision=revision)).read_text(encoding="utf-8"))
def upload_json(path, value, message):
    client, repo, _ = api(); client.upload_file(path_or_fileobj=(json.dumps(value,ensure_ascii=False,indent=2)+"\n").encode(),path_in_repo=path,repo_id=repo,repo_type="dataset",commit_message=message)
def output_root(plan_id, number): return f"_system/full_merges/{plan_id}/output-{number:03d}"

def next_hourly_scan(target, minute=17):
    candidate = target.replace(minute=minute, second=0, microsecond=0)
    if candidate < target:
        candidate += timedelta(hours=1)
    return candidate

def write_phase1_summary(state, title):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    taipei = ZoneInfo("Asia/Taipei")
    paused = datetime.fromisoformat(state["paused_at"]).astimezone(taipei)
    target = datetime.fromisoformat(state["target_resume_at"]).astimezone(taipei)
    scan = next_hourly_scan(target)
    confirmed_percent = (int(state["confirmed_bytes"]) / int(state["total_size"]) * 100)
    text = (
        "## YouTube 兩階段上傳狀態\n\n"
        "✅ 本次流程成功：影片已合併並暫停在 YouTube 續傳工作階段。\n\n"
        f"- 影片：{title}\n"
        f"- 目前進度：{confirmed_percent:.2f}%（尚未完成發布）\n"
        f"- 暫停時間（台北）：{paused:%Y-%m-%d %H:%M:%S}\n"
        f"- 可開始續傳（台北）：{target:%Y-%m-%d %H:%M:%S}\n"
        f"- 預計排程啟動 Phase 2（台北）：{scan:%Y-%m-%d %H:%M:%S}\n"
        "- 下一步：`hf-upload-resume-scheduler.yml` 將自動觸發 `resume-hf-upload.yml`，補完最後 2%、設定封面並完成 YouTube 發布。\n"
        "- 注意：此 run 顯示 Success 只代表 Phase 1 成功，不代表 YouTube 影片已完整發布。\n"
    )
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(text)

def ffconcat_text(urls, token):
    return "ffconcat version 1.0\n" + "".join(
        "file '" + url.replace("'", "%27") + "'\n"
        "option headers 'Authorization: Bearer " + token + "'\n"
        for url in urls
    )

def local_ffconcat_text(paths):
    """Build a concat list from fully downloaded files, never remote URLs."""
    return "ffconcat version 1.0\n" + "".join(
        "file '" + Path(path).resolve().as_posix().replace("'", "'\\''") + "'\n"
        for path in paths
    )

def download_pinned_part(repo, path, revision, token, local_dir, retries=SOURCE_DOWNLOAD_RETRIES):
    """Download one immutable source Part with bounded exponential backoff."""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return Path(hf_hub_download(
                repo, path, repo_type="dataset", token=token, revision=revision,
                local_dir=local_dir,
            ))
        except Exception as error:
            last_error = error
            if attempt < retries:
                delay = min(2 ** (attempt - 1), 60)
                print(
                    f"HF source download failed ({attempt}/{retries}) for {path}; "
                    f"retrying in {delay}s: {error}",
                    file=sys.stderr, flush=True,
                )
                time.sleep(delay)
    raise RuntimeError(
        f"HF source download failed after {retries} attempts for {path}: {last_error}"
    ) from last_error

def verify_complete_video(path, expected_duration):
    """Reject incomplete/corrupt merges before they can be published or uploaded."""
    validation = validate_video(path, audio_duration=expected_duration)
    duration_delta = abs(float(validation["duration_seconds"]) - float(expected_duration))
    duration_tolerance = max(2.0, float(expected_duration) * 0.0001)
    if duration_delta > duration_tolerance:
        raise RuntimeError(
            f"merged video duration mismatch: expected {expected_duration:.3f}s, "
            f"got {validation['duration_seconds']:.3f}s"
        )
    subprocess.run([
        "ffmpeg", "-v", "error", "-xerror", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "null", "-",
    ], check=True)
    return validation

def source_revision(manifest):
    revision = str(manifest.get("output_revision") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("merge manifest has no immutable source revision")
    return revision


def resolve_url(path, revision):
    repo,_=repo_token()
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{urllib.parse.quote(path,safe='/')}"


def read_range(url,start,end, retries=TRANSFER_RETRIES):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url,headers={"Authorization":f"Bearer {repo_token()[1]}","Range":f"bytes={start}-{end}"},stream=True,timeout=(30,180)) as response:
                if response.status_code!=206: raise RuntimeError(f"HF did not honor byte range: HTTP {response.status_code}")
                data=response.raw.read(end-start+1)
            if len(data)!=end-start+1: raise RuntimeError("HF range response was truncated")
            return data
        except (requests.RequestException, OSError, RuntimeError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"HF range download failed after {retries} attempts: {last_error}") from last_error


def verified_hf_source(manifest):
    """Read the immutable HF object completely and verify it before contacting YouTube."""
    if manifest.get("status") != "merge_complete":
        raise RuntimeError("merge manifest is not complete")
    expected_size = int(manifest.get("bytes") or 0)
    expected_sha256 = str(manifest.get("sha256") or "").lower()
    if expected_size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("merge manifest has no valid size/checksum")
    source = resolve_url(manifest["video_path"], source_revision(manifest))
    digest = hashlib.sha256(); received = 0
    while received < expected_size:
        block = read_range(source, received, min(expected_size - 1, received + CHUNK - 1))
        digest.update(block); received += len(block)
    if received != expected_size:
        raise RuntimeError(f"HF merged video size mismatch: expected {expected_size}, got {received}")
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError("HF merged video checksum mismatch")
    return source

def make_plan(args):
    repo, token = repo_token(); books=HfCatalog(repo,token).list_books(revision=args.revision or None); book=next((b for b in books if b.key==args.book_key),None)
    if not book or not book.mergeable: raise RuntimeError(f"book is not mergeable: {args.book_key}")
    if args.revision and book.revision != args.revision: raise RuntimeError("HF revision changed after GUI preview; refresh the GUI")
    plan=build_plan(book,None if args.mode=="all" else args.max_hours)
    if args.expected_plan_id and plan["plan_id"] != args.expected_plan_id: raise RuntimeError("GUI and Actions merge plans differ")
    Path(args.plan).write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
    matrix={"include":[{"output_number":x["output_number"]} for x in plan["outputs"]]}
    with open(os.environ["GITHUB_OUTPUT"],"a",encoding="utf-8") as f: f.write("matrix="+json.dumps(matrix,separators=(",",":"))+"\nplan_id="+plan["plan_id"]+"\n")

def merge_output(args):
    plan=json.loads(Path(args.plan).read_text(encoding="utf-8")); item=next(x for x in plan["outputs"] if x["output_number"]==args.output_number)
    _, repo, token=api(); root=output_root(plan["plan_id"],args.output_number); target=Path(args.bucket_mount)/root/"audiobook.mp4"; target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        source_dir=Path(tmp)/"source-parts"
        local_parts=[
            download_pinned_part(repo,part["video_path"],plan["repo_revision"],token,source_dir)
            for part in item["parts"]
        ]
        concat=Path(tmp)/"parts.ffconcat"
        concat.write_text(local_ffconcat_text(local_parts),encoding="utf-8")
        subprocess.run(["ffmpeg","-hide_banner","-y","-f","concat","-safe","0","-i",str(concat),"-map","0:v:0","-map","0:a:0","-c","copy",str(target)],check=True)
    validation=verify_complete_video(target,item["duration_seconds"])
    manifest={"status":"merge_complete","plan_id":plan["plan_id"],"parts_revision":plan["repo_revision"],"output":item,"full_chapter_timeline":_output_chapters(item),"book_title":plan["book_title"],"youtube_title":item["youtube_title"],"youtube_description":output_chapter_timeline(item),"cover_path":f"{plan['book_root']}/master_cover.jpg","video_path":f"{root}/audiobook.mp4","bytes":validation["bytes"],"sha256":validation["sha256"],"media_info":validation}
    manifest_file=Path(args.bucket_mount,root,"merge_manifest.json"); manifest_file.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    client, repo, _ = api()
    commit=client.create_commit(repo_id=repo,repo_type="dataset",commit_message=f"Publish full merge {plan['plan_id']} output {args.output_number}",operations=[CommitOperationAdd(path_in_repo=manifest["video_path"],path_or_fileobj=str(target)),CommitOperationAdd(path_in_repo=f"{root}/merge_manifest.json",path_or_fileobj=str(manifest_file))])
    manifest["output_revision"] = str(commit.oid)
    upload_json(f"{root}/merge_manifest.json", manifest, f"Pin full merge {plan['plan_id']} output {args.output_number}")

def credentials(slot=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    slots = [int(slot)] if slot else range(1,11)
    for n in slots:
        values=[os.getenv(f"YOUTUBE_{key}_{n}","") for key in ("CLIENT_ID","CLIENT_SECRET","REFRESH_TOKEN")]
        if all(values):
            cred=Credentials(None,refresh_token=values[2],token_uri="https://oauth2.googleapis.com/token",client_id=values[0],client_secret=values[1]); cred.refresh(Request()); return cred
    raise RuntimeError("No YouTube credentials")

def put_chunk(url,start,total,data): return requests.put(url,headers={"Content-Length":str(len(data)),"Content-Range":f"bytes {start}-{start+len(data)-1}/{total}"},data=data,timeout=180)
def query(url,total,cred):
    response=requests.put(url,headers={"Authorization":f"Bearer {cred.token}","Content-Length":"0","Content-Range":f"bytes */{total}"},timeout=60)
    if response.status_code in (200,201): return total,(response.json() or {}).get("id")
    if response.status_code!=308: raise RuntimeError(f"YouTube resume query failed: {response.status_code} {response.text[:300]}")
    value=response.headers.get("Range",""); return (int(value.rsplit("-",1)[1])+1 if "-" in value else 0),None

def phase1(args):
    manifest=remote_json(args.manifest); total=int(manifest["bytes"]); source=verified_hf_source(manifest); cred=credentials(getattr(args,"credential_slot",None))
    headers={"Authorization":f"Bearer {cred.token}","Content-Type":"application/json; charset=UTF-8","X-Upload-Content-Length":str(total),"X-Upload-Content-Type":"video/mp4"}
    title = str(args.title or manifest.get("youtube_title") or "").strip()
    description = str(manifest.get("youtube_description") or "").strip()
    if not title or not description:
        raise RuntimeError("merge manifest is missing YouTube title or chapter timeline")
    if len(description) > YOUTUBE_DESCRIPTION_LIMIT:
        description = output_chapter_timeline(manifest["output"])
    if len(description) > YOUTUBE_DESCRIPTION_LIMIT:
        raise RuntimeError("cannot build a legal YouTube description")
    if len(title) > 100:
        raise RuntimeError("YouTube title exceeds 100 characters")
    body={"snippet":{"title":title,"description":description},"status":{"privacyStatus":args.privacy}}
    response=requests.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",headers=headers,json=body,timeout=60); response.raise_for_status(); session=response.headers["Location"]
    target=(int(total*.98)//(256*1024))*(256*1024); sent=0
    while sent<target:
        end=min(target-1,sent+CHUNK-1); data=read_range(source,sent,end)
        result=put_chunk(session,sent,total,data)
        if result.status_code not in (200,201,308): raise RuntimeError(f"YouTube chunk failed: {result.status_code}")
        sent += len(data)
    confirmed,video_id=query(session,total,cred); now=datetime.now(timezone.utc)
    state={"status":"paused_at_98","book_title":manifest.get("book_title"),"youtube_title":manifest.get("youtube_title"),"plan_id":manifest.get("plan_id"),"output_number":(manifest.get("output") or {}).get("output_number"),"session_url":session,"session_id":hashlib.sha256(session.encode()).hexdigest()[:20],"manifest_path":args.manifest,"source_revision":source_revision(manifest),"total_size":total,"confirmed_bytes":confirmed,"paused_at":now.isoformat(),"target_resume_at":(now+timedelta(hours=24)).isoformat(),"privacy":args.privacy,"video_id":video_id,"credential_slot":int(getattr(args,"credential_slot",None) or 1)}
    upload_json(args.state_path,state,"Save two-phase YouTube session")
    write_phase1_summary(state, title)

def phase2(args):
    state=remote_json(args.state_path); manifest=remote_json(state["manifest_path"]); total=int(manifest["bytes"])
    if total!=int(state["total_size"]) or source_revision(manifest)!=state.get("source_revision"): raise RuntimeError("HF merged video changed")
    cred=credentials(state.get("credential_slot")); sent,video_id=query(state["session_url"],total,cred); source=resolve_url(manifest["video_path"],source_revision(manifest))
    while sent<total:
        end=min(total-1,sent+CHUNK-1); data=read_range(source,sent,end)
        response=put_chunk(state["session_url"],sent,total,data)
        if response.status_code in (200,201): video_id=(response.json() or {}).get("id")
        elif response.status_code!=308: raise RuntimeError(f"YouTube chunk failed: {response.status_code}")
        sent += len(data)
    if not video_id: _,video_id=query(state["session_url"],total,cred)
    if not video_id: raise RuntimeError("Upload completed without a YouTube video id")
    cover=hf_hub_download(repo_token()[0],manifest["cover_path"],repo_type="dataset",token=repo_token()[1])
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    build("youtube","v3",credentials=cred).thumbnails().set(videoId=video_id,media_body=MediaFileUpload(cover,mimetype="image/jpeg")).execute()
    state.update(status="complete",video_id=video_id,video_url=f"https://www.youtube.com/watch?v={video_id}",completed_at=datetime.now(timezone.utc).isoformat()); upload_json(args.state_path,state,"Complete two-phase YouTube upload")

def scan_due(_args):
    client, repo, token=api(); now=datetime.now(timezone.utc); files=client.list_repo_files(repo,repo_type="dataset")
    for path in files:
        if not path.startswith("_system/full_merges/") or not path.endswith("/upload_state.json"): continue
        state=remote_json(path)
        if state.get("status")!="paused_at_98" or datetime.fromisoformat(state["target_resume_at"])>now: continue
        title = state.get("book_title")
        if not title and state.get("manifest_path"):
            try:
                title = remote_json(state["manifest_path"]).get("book_title")
            except Exception:
                pass
        if title and not state.get("book_title"):
            state["book_title"] = title
        state["status"]="resume_dispatched"; state["resume_attempts"]=int(state.get("resume_attempts") or 0)+1; state["resume_dispatched_at"]=now.isoformat(); upload_json(path,state,"Dispatch phase 2")
        response=requests.post(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/resume-hf-upload.yml/dispatches",headers={"Authorization":f"Bearer {os.environ['GITHUB_TOKEN']}","Accept":"application/vnd.github+json"},json={"ref":os.environ.get("GITHUB_REF_NAME","master"),"inputs":{"state_path":path,"book_title":str(state.get("book_title") or "")}},timeout=30)
        if response.status_code not in (204,):
            state["status"]="paused_at_98"; state["dispatch_error"]=f"HTTP {response.status_code}: {response.text[:300]}"; upload_json(path,state,"Restore failed phase 2 dispatch")
            raise RuntimeError(f"resume dispatch failed: {response.status_code} {response.text}")

def reset_resume(args):
    state=remote_json(args.state_path); attempts=int(state.get("resume_attempts") or 0)
    state["status"]="needs_attention" if attempts>=3 else "paused_at_98"
    state["last_resume_failure_at"]=datetime.now(timezone.utc).isoformat()
    upload_json(args.state_path,state,"Record failed phase 2 attempt")

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("plan","merge","phase1","phase2","scan","reset")); p.add_argument("--book-key"); p.add_argument("--revision",default=""); p.add_argument("--mode",choices=("all","max_hours")); p.add_argument("--max-hours",type=float); p.add_argument("--expected-plan-id",default=""); p.add_argument("--plan",default="plan.json"); p.add_argument("--output-number",type=int); p.add_argument("--bucket-mount"); p.add_argument("--manifest"); p.add_argument("--state-path"); p.add_argument("--privacy",choices=("private","unlisted","public"),default="public"); p.add_argument("--title",default=""); p.add_argument("--credential-slot",type=int,choices=range(1,11)); a=p.parse_args()
    {"plan":make_plan,"merge":merge_output,"phase1":phase1,"phase2":phase2,"scan":scan_due,"reset":reset_resume}[a.command](a)
if __name__=="__main__": main()
