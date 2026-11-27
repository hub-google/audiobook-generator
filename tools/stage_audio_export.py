"""Stage a small AUDIO export in private storage so Pages needs no GitHub popup.

The OIDC identity is restricted by the Edge Function to this repo/workflow.
Large exports remain in the original GitHub Artifact; never silently truncate them.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EDGE="https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-web-commands"
LIMIT=45_000_000

def main():
    request_id=os.environ.get("EXPORT_REQUEST_ID","")
    matched=re.fullmatch(r"issue-(\d+)",request_id)
    if not matched:
        print("Not a website-initiated export; preserving original GitHub Artifact")
        return
    command_id=int(matched.group(1))-1_000_000_000
    if command_id<=0:
        print("Not an authenticated website command; preserving original GitHub Artifact")
        return
    output=Path("audio-export.zip")
    size=output.stat().st_size
    if size>LIMIT:
        print(f"AUDIO_SITE_DOWNLOAD_UNAVAILABLE: {size} bytes exceeds secure 45 MB single-file limit")
        return
    oidc_url=os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    oidc_url+=("&" if "?" in oidc_url else "?")+urlencode({"audience":"audio-web-commands"})
    identity=Request(oidc_url,headers={
        "Authorization":"Bearer "+os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    })
    with urlopen(identity,timeout=25) as stream:
        token=json.load(stream)["value"]
    name=f"audio-export-{command_id}.zip"
    req=Request(EDGE+"/stage",data=output.read_bytes(),method="POST",headers={
        "Authorization":"Bearer "+token,
        "Content-Type":"application/zip",
        "X-Audio-Command-Id":str(command_id),
        "X-Audio-Filename":name
    })
    with urlopen(req,timeout=140) as response:
        result=json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Secure export staging did not succeed")
    print(f"AUDIO site download is ready; private asset {result['asset']['id']}")

if __name__=="__main__":
    main()
