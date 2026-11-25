"""Persistent evidence of speech failures; legacy silence must not be resumed as speech."""
import json
import os
from pathlib import Path

def degraded_chapters(path):
    path = Path(path)
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["chapter"]) for row in data.get("segments", []) if row.get("silent_fallback_used") and row.get("chapter") is not None}

def update_fallback_manifest(audio_dir, new_records=(), recovered_chapter=None):
    path = Path(audio_dir) / "silent-fallback-manifest.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"segments": []}
    rows = list(old.get("segments", []))
    if recovered_chapter is not None:
        rows = [row for row in rows if str(row.get("chapter")) != str(recovered_chapter)]
    for row in new_records:
        key = (str(row.get("chapter")), row.get("segment_index"), row.get("original_text_hash"))
        rows = [prior for prior in rows if (str(prior.get("chapter")), prior.get("segment_index"), prior.get("original_text_hash")) != key]
        rows.append(row)
    if not rows and not path.exists():
        return
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps({"schema_version": 1, "count": len(rows), "segments": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
