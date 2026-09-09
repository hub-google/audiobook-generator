"""GitHub Actions backend for HF merge plans and two-phase YouTube uploads."""
from __future__ import annotations

import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_catalog import HfCatalog
from merge_plan import build_plan

CHUNK = 8 * 1024 * 1024
YOUTUBE_DESCRIPTION_LIMIT = 5000


def output_chapter_timeline(item):
    chapters = [chapter for part in item["parts"] for chapter in part.get("chapter_timeline") or []]
    if not chapters:
        raise RuntimeError("merge output has no chapter timeline")
    from src.youtube_upload.metadata import build_chapter_timeline
    timeline = build_chapter_timeline(chapters)
    if len(timeline) <= YOUTUBE_DESCRIPTION_LIMIT:
        return timeline
    # Very long compilations still retain every timestamp. Compact only the
    # labels when YouTube's 5,000-character description limit requires it.
    compact = [timeline.splitlines()[0]]
    for line in timeline.splitlines()[1:]:
        timestamp, _, label = line.partition(" ")
        number = re.search(r"\d+", label)
        compact.append(f"{timestamp} {number.group(0) if number else label}")
    result = "\n".join(compact)
    if len(result) > YOUTUBE_DESCRIPTION_LIMIT:
        raise RuntimeError("complete chapter timeline exceeds YouTube's 5,000-character description limit")
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

def make_plan(args):
    repo, token = repo_token(); books=HfCatalog(repo,token).list_books(); book=next((b for b in books if b.key==args.book_key),None)
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
    urls=[]
    for part in item["parts"]:
        encoded=urllib.parse.quote(part["video_path"],safe="/")
        urls.append(f"https://huggingface.co/datasets/{repo}/resolve/{plan['repo_revision']}/{encoded}")
    with tempfile.TemporaryDirectory() as tmp:
        concat=Path(tmp)/"parts.ffconcat"; concat.write_text("ffconcat version 1.0\n"+"".join("file '"+u.replace("'","%27")+"'\n" for u in urls),encoding="utf-8")
        subprocess.run(["ffmpeg","-hide_banner","-y","-headers",f"Authorization: Bearer {token}\r\n","-protocol_whitelist","file,http,https,tcp,tls,crypto","-f","concat","-safe","0","-i",str(concat),"-map","0:v:0","-map","0:a:0","-c","copy",str(target)],check=True)
    probe=json.loads(subprocess.run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(target)],capture_output=True,text=True,check=True).stdout)
    digest=hashlib.sha256()
    with target.open("rb") as f:
        for block in iter(lambda:f.read(CHUNK),b""): digest.update(block)
    manifest={"status":"merge_complete","plan_id":plan["plan_id"],"output":item,"book_title":plan["book_title"],"youtube_title":item["youtube_title"],"youtube_description":output_chapter_timeline(item),"cover_path":f"{plan['book_root']}/master_cover.jpg","video_path":f"{root}/audiobook.mp4","bytes":target.stat().st_size,"sha256":digest.hexdigest(),"media_info":probe}
    manifest_file=Path(args.bucket_mount,root,"merge_manifest.json"); manifest_file.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    client, repo, _ = api()
    client.create_commit(repo_id=repo,repo_type="dataset",commit_message=f"Publish full merge {plan['plan_id']} output {args.output_number}",operations=[CommitOperationAdd(path_in_repo=manifest["video_path"],path_or_fileobj=str(target)),CommitOperationAdd(path_in_repo=f"{root}/merge_manifest.json",path_or_fileobj=str(manifest_file))])

def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    for n in range(1,11):
        values=[os.getenv(f"YOUTUBE_{key}_{n}","") for key in ("CLIENT_ID","CLIENT_SECRET","REFRESH_TOKEN")]
        if all(values):
            cred=Credentials(None,refresh_token=values[2],token_uri="https://oauth2.googleapis.com/token",client_id=values[0],client_secret=values[1]); cred.refresh(Request()); return cred
    raise RuntimeError("No YouTube credentials")

def resolve_url(path):
    repo,_=repo_token(); return f"https://huggingface.co/datasets/{repo}/resolve/main/{urllib.parse.quote(path,safe='/')}"
def put_chunk(url,start,total,data): return requests.put(url,headers={"Content-Length":str(len(data)),"Content-Range":f"bytes {start}-{start+len(data)-1}/{total}"},data=data,timeout=180)
def read_range(url,start,end):
    with requests.get(url,headers={"Authorization":f"Bearer {repo_token()[1]}","Range":f"bytes={start}-{end}"},stream=True,timeout=180) as response:
        if response.status_code!=206: raise RuntimeError(f"HF did not honor byte range: HTTP {response.status_code}")
        data=response.raw.read(end-start+1)
    if len(data)!=end-start+1: raise RuntimeError("HF range response was truncated")
    return data
def query(url,total,cred):
    response=requests.put(url,headers={"Authorization":f"Bearer {cred.token}","Content-Length":"0","Content-Range":f"bytes */{total}"},timeout=60)
    if response.status_code in (200,201): return total,(response.json() or {}).get("id")
    if response.status_code!=308: raise RuntimeError(f"YouTube resume query failed: {response.status_code} {response.text[:300]}")
    value=response.headers.get("Range",""); return (int(value.rsplit("-",1)[1])+1 if "-" in value else 0),None

def phase1(args):
    manifest=remote_json(args.manifest); total=int(manifest["bytes"]); source=resolve_url(manifest["video_path"]); cred=credentials()
    headers={"Authorization":f"Bearer {cred.token}","Content-Type":"application/json; charset=UTF-8","X-Upload-Content-Length":str(total),"X-Upload-Content-Type":"video/mp4"}
    title = str(args.title or manifest.get("youtube_title") or "").strip()
    description = str(manifest.get("youtube_description") or "").strip()
    if not title or not description:
        raise RuntimeError("merge manifest is missing YouTube title or chapter timeline")
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
    state={"status":"paused_at_98","session_url":session,"session_id":hashlib.sha256(session.encode()).hexdigest()[:20],"manifest_path":args.manifest,"total_size":total,"confirmed_bytes":confirmed,"paused_at":now.isoformat(),"target_resume_at":(now+timedelta(hours=24)).isoformat(),"privacy":args.privacy,"video_id":video_id}
    upload_json(args.state_path,state,"Save two-phase YouTube session")

def phase2(args):
    state=remote_json(args.state_path); manifest=remote_json(state["manifest_path"]); total=int(manifest["bytes"])
    if total!=int(state["total_size"]): raise RuntimeError("HF merged video changed")
    cred=credentials(); sent,video_id=query(state["session_url"],total,cred); source=resolve_url(manifest["video_path"])
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
        response=requests.post(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/resume-hf-upload.yml/dispatches",headers={"Authorization":f"Bearer {os.environ['GITHUB_TOKEN']}","Accept":"application/vnd.github+json"},json={"ref":os.environ.get("GITHUB_REF_NAME","main"),"inputs":{"state_path":path}},timeout=30)
        if response.status_code not in (204,): raise RuntimeError(f"resume dispatch failed: {response.status_code} {response.text}")
        state["status"]="resume_dispatched"; state["resume_attempts"]=int(state.get("resume_attempts") or 0)+1; state["resume_dispatched_at"]=now.isoformat(); upload_json(path,state,"Dispatch phase 2")

def reset_resume(args):
    state=remote_json(args.state_path); attempts=int(state.get("resume_attempts") or 0)
    state["status"]="needs_attention" if attempts>=3 else "paused_at_98"
    state["last_resume_failure_at"]=datetime.now(timezone.utc).isoformat()
    upload_json(args.state_path,state,"Record failed phase 2 attempt")

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=("plan","merge","phase1","phase2","scan","reset")); p.add_argument("--book-key"); p.add_argument("--revision",default=""); p.add_argument("--mode",choices=("all","max_hours")); p.add_argument("--max-hours",type=float); p.add_argument("--expected-plan-id",default=""); p.add_argument("--plan",default="plan.json"); p.add_argument("--output-number",type=int); p.add_argument("--bucket-mount"); p.add_argument("--manifest"); p.add_argument("--state-path"); p.add_argument("--privacy",default="public"); p.add_argument("--title",default=""); a=p.parse_args()
    {"plan":make_plan,"merge":merge_output,"phase1":phase1,"phase2":phase2,"scan":scan_due,"reset":reset_resume}[a.command](a)
if __name__=="__main__": main()
