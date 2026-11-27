"""Securely stage AUDIO exports as one or more ordered private ZIP chunks.

Uses GitHub Actions OIDC only; never ships GitHub write credentials to Pages.
Splitting avoids Supabase's per-object upload limit without losing the archive.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

EDGE = "https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-web-commands"
CHUNK_SIZE = 25_000_000


def chunk_names(base, total_size, chunk_size=CHUNK_SIZE):
    """Return (filename, byte offset, byte count) for every ordered chunk."""
    if total_size < 1 or chunk_size < 1:
        raise ValueError("Empty ZIP or invalid chunk size")
    parts = math.ceil(total_size / chunk_size)
    if parts > 99999:
        raise ValueError("ZIP contains too many chunks for safe transport")
    for i in range(parts):
        offset = i * chunk_size
        name = base if parts == 1 else f"{base}.part{i+1:05d}-of-{parts:05d}"
        yield name, offset, min(chunk_size, total_size - offset)


def stage_export(output, command_id, token, *, chunk_size=CHUNK_SIZE):
    size = output.stat().st_size
    if not zipfile_valid(output):
        raise ValueError("Export archive is not a ZIP")
    names = list(chunk_names(f"audio-export-{command_id}.zip", size, chunk_size))
    with output.open("rb") as stream:
        for i, (name, offset, length) in enumerate(names, 1):
            assert stream.tell() == offset
            chunk = stream.read(length)
            if len(chunk) != length:
                raise IOError("Export archive unexpectedly truncated")
            req = Request(EDGE + "/stage", data=chunk, method="POST", headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/zip",
                "X-Audio-Command-Id": str(command_id),
                "X-Audio-Filename": name,
            })
            with urlopen(req, timeout=180) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise RuntimeError("Secure export staging failed")
            print(f"[AUDIO_PRIVATE_ZIP] staged {i}/{len(names)} {name} ({length} bytes)", flush=True)
    return len(names)


def zipfile_valid(path):
    import zipfile
    return zipfile.is_zipfile(path)


def main():
    request_id = os.environ.get("EXPORT_REQUEST_ID", "")
    matched = re.fullmatch(r"issue-(\d+)", request_id)
    if not matched:
        print("Not a website-initiated export; preserving original GitHub Artifact")
        return
    command_id = int(matched.group(1)) - 1_000_000_000
    if command_id <= 0:
        print("Not an authenticated website command; preserving original GitHub Artifact")
        return
    oidc_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    oidc_url += ("&" if "?" in oidc_url else "?") + urlencode({"audience": "audio-web-commands"})
    identity = Request(oidc_url, headers={
        "Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    })
    with urlopen(identity, timeout=25) as stream:
        token = json.load(stream)["value"]
    count = stage_export(Path("audio-export.zip"), command_id, token)
    print(f"AUDIO site export ready: {count} private chunk(s)", flush=True)


if __name__ == "__main__":
    main()
