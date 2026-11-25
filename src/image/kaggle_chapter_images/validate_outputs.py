"""Validate variable-size chapter illustration batches before publication."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from PIL import Image

def validate_output(root):
    root = Path(root)
    report = json.loads((root / "run_report.json").read_text(encoding="utf-8"))
    if report["status"] != "generated":
        raise ValueError("Kaggle batch did not complete")
    def rows(name):
        return [json.loads(line) for line in (root / "manifests" / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    chapters, images = rows("chapters.jsonl"), rows("images.jsonl")
    if len(chapters) != report["chapters"] or len(images) != report["images"]:
        raise ValueError("Kaggle report/manifest count mismatch")
    if not chapters or not images:
        raise ValueError("No publishable chapter illustrations")
    chapter_ids = [int(row["chapter"]) for row in chapters]
    if len(set(chapter_ids)) != len(chapter_ids):
        raise ValueError("Duplicate chapters")
    expected = {}
    for row in images:
        ch, scene = int(row["chapter"]), int(row["scene"])
        path = f"chapter_{ch:04d}/scene_{scene:02d}.webp"
        if path in expected or ch not in chapter_ids or scene not in (1, 2):
            raise ValueError("Duplicate or unexpected image identity")
        if row["status"] != "needs_review" or not str(row["relative_path"]).endswith("/" + path):
            raise ValueError("Image must retain review status and matching path")
        expected[path] = row
    actual = {path.relative_to(root).as_posix() for path in root.glob("chapter_*/scene_*.webp")}
    if actual != set(expected):
        raise ValueError("Kaggle image inventory mismatch")
    for row in chapters:
        ch = int(row["chapter"])
        matched = [r for r in images if int(r["chapter"]) == ch]
        if row["status"] == "needs_review":
            if len(matched) != 2 or set(row["scene_paths"]) != {r["relative_path"] for r in matched}:
                raise ValueError("Chapter does not have its required two scenes")
            if any(r["source_sha256"] != row["source_sha256"] for r in matched):
                raise ValueError("Image source does not match chapter")
        elif matched or row.get("scene_paths"):
            raise ValueError("Skipped chapter unexpectedly has images")
    for path, row in expected.items():
        local = root / path
        with Image.open(local) as opened:
            if opened.size != (1280, 720) or opened.format != "WEBP":
                raise ValueError(f"Invalid image format: {path}")
            opened.verify()
        if hashlib.sha256(local.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"Image checksum mismatch: {path}")
    return report
