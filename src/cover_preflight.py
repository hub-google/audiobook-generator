"""Generate the reviewable book cover and Gemini prompt as a preflight asset."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
from pathlib import Path

import yaml

try:
    from .book_profiles import book_profile_id
    from .metadata_gen import ensure_master_cover
    from .source_identity import source_fingerprint, workspace_name
except ImportError:
    from book_profiles import book_profile_id
    from metadata_gen import ensure_master_cover
    from source_identity import source_fingerprint, workspace_name


def build_cover_config(book_title, catalog_url, profile_snapshot_b64=""):
    """Build only the identity/settings needed by cover preflight.

    Cover generation must not parse or fetch the chapter catalog.  The URL is
    retained solely as a stable storage identity.
    """
    snapshot = {}
    if profile_snapshot_b64:
        try:
            snapshot = json.loads(base64.b64decode(profile_snapshot_b64).decode("utf-8"))
        except Exception as error:
            raise ValueError(f"無法解碼書籍設定快照：{error}") from error
        if not isinstance(snapshot, dict):
            raise ValueError("書籍設定快照必須是 JSON 物件")

    fingerprint = source_fingerprint(catalog_url)
    if snapshot.get("catalog_url") and source_fingerprint(snapshot["catalog_url"]) != fingerprint:
        raise ValueError("書籍設定快照屬於另一個來源網址")
    expected_profile_id = book_profile_id(catalog_url)
    if snapshot.get("book_profile_id") and snapshot["book_profile_id"] != expected_profile_id:
        raise ValueError("書籍設定快照的識別碼不符")

    return {
        "book_title": str(book_title).strip(),
        "catalog_url": catalog_url,
        "source_fingerprint": fingerprint,
        "book_profile_id": expected_profile_id,
        "profile_revision": int(snapshot.get("profile_revision") or 0),
        "manual_cover": dict(snapshot.get("manual_cover") or {}),
        "paths": {"workspace_base": "Workspace"},
        # Explicitly prevent cover metadata discovery from contacting the novel
        # source. It may use its title-based research fallbacks instead.
        "cover_catalog_lookup": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--restore")
    parser.add_argument("--book-title")
    parser.add_argument("--catalog-url")
    parser.add_argument("--book-profile-snapshot-b64", default="")
    args = parser.parse_args()
    config_path = Path(args.config)
    if args.book_title or args.catalog_url:
        if not args.book_title or not args.catalog_url:
            parser.error("--book-title 與 --catalog-url 必須一起提供")
        config = build_cover_config(
            args.book_title, args.catalog_url, args.book_profile_snapshot_b64,
        )
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
    else:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
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
    if config.get("cover_catalog_lookup", True):
        os.environ["BOOK_CATALOG_URL"] = config["catalog_url"]
    else:
        os.environ.pop("BOOK_CATALOG_URL", None)
    catalog_metadata = (config.get("catalog_snapshot") or {}).get("metadata") or {}
    if catalog_metadata:
        os.environ["BOOK_CATALOG_METADATA"] = json.dumps(catalog_metadata, ensure_ascii=False)
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
