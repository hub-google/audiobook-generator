"""Generate the reviewable book cover and Gemini prompt as a preflight asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import yaml

try:
    from .metadata_gen import ensure_master_cover
    from .source_identity import workspace_name
except ImportError:
    from metadata_gen import ensure_master_cover
    from source_identity import workspace_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--restore")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    root = Path(config["paths"]["workspace_base"]) / workspace_name(config)
    if args.restore:
        manifests = list(Path(args.restore).rglob("cover-preflight-manifest.json"))
        if len(manifests) != 1:
            raise RuntimeError("Cover manifest missing or ambiguous")
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        if (manifest.get("artifact_role") != "cover_preflight" or
                manifest.get("task_id") != args.task_id or
                manifest.get("book_profile_id") != config.get("book_profile_id")):
            raise RuntimeError("Cover task/profile identity mismatch")
        folder = manifests[0].parent
        for name, key in (("master_cover.jpg", "cover_sha256"), ("master_cover_prompt.json", "prompt_sha256")):
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != manifest[key]:
                raise RuntimeError("Approved cover checksum mismatch")
        shutil.copytree(folder, root / "Cover", dirs_exist_ok=True)
        return
    os.environ["BOOK_CATALOG_URL"] = config["catalog_url"]
    os.environ["COVER_REVIEW_OUTPUT"] = str(root / "Cover")
    if config.get("manual_cover"):
        from cover_assets import restore_from_config
        restore_from_config(args.config)
        prompt_path = root / "Cover" / "master_cover_prompt.json"
        prompt_path.write_text(json.dumps({"book_title": config["book_title"], "source": "manual", "prompt": "使用者指定封面，未呼叫 AI 生圖"}, ensure_ascii=False), encoding="utf-8")
    cover, _ = ensure_master_cover(config["book_title"], str(root))
    cover_path = Path(cover)
    prompt_path = root / "Cover" / "master_cover_prompt.json"
    manifest = {
        "schema_version": 1,
        "artifact_role": "cover_preflight",
        "task_id": args.task_id,
        "book_profile_id": config.get("book_profile_id"),
        "source_fingerprint": config.get("source_fingerprint"),
        "cover_sha256": hashlib.sha256(cover_path.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        "cover": str(cover_path.as_posix()),
        "prompt": str(prompt_path.as_posix()),
    }
    output = root / "Cover" / "cover-preflight-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
