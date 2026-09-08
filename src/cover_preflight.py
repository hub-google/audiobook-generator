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
    from .metadata_gen import (
        COVER_TEMPLATE_VERSION,
        download_ai_image,
        generate_gemini_book_introduction,
        generate_gemini_cover_information,
        review_cover_information,
        save_process_log,
    )
    from .source_identity import source_fingerprint, workspace_name
except ImportError:
    from book_profiles import book_profile_id
    from metadata_gen import (
        COVER_TEMPLATE_VERSION,
        download_ai_image,
        generate_gemini_book_introduction,
        generate_gemini_cover_information,
        review_cover_information,
        save_process_log,
    )
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


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"無法讀取封面階段產物 {path}：{error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"封面階段產物必須是 JSON 物件：{path}")
    return value


def _stage_paths(config):
    root = Path(config["paths"]["workspace_base"]) / workspace_name(config)
    cover_dir = root / "Cover"
    return root, cover_dir


def _run_introduction_stage(args, config_path: Path):
    config = build_cover_config(
        args.book_title, args.catalog_url, args.book_profile_snapshot_b64,
    )
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    _, cover_dir = _stage_paths(config)
    cover_dir.mkdir(parents=True, exist_ok=True)

    # Preserve the existing manual-cover shortcut: a verified user image skips
    # every paid Gemini/HF call while flowing through the same four jobs.
    try:
        from .cover_assets import restore_from_config
    except ImportError:
        from cover_assets import restore_from_config
    if config.get("manual_cover") and restore_from_config(str(config_path)):
        _write_json(
            cover_dir / "master_cover_prompt.json",
            {"book_title": config["book_title"], "source": "manual", "prompt": "使用者指定封面，未呼叫 AI 生圖"},
        )
        _write_json(cover_dir / "book_introduction.json", {"manual_cover": True})
        return

    introduction = generate_gemini_book_introduction(config["book_title"])
    _write_json(
        cover_dir / "book_introduction.json",
        {"manual_cover": False, "introduction": introduction},
    )


def _run_cover_information_stage(config):
    _, cover_dir = _stage_paths(config)
    introduction_record = _read_json(cover_dir / "book_introduction.json")
    if introduction_record.get("manual_cover"):
        _write_json(cover_dir / "cover_information_draft.json", {"manual_cover": True})
        return

    introduction = str(introduction_record.get("introduction") or "")
    os.environ["COVER_REVIEW_OUTPUT"] = str(cover_dir)
    draft = generate_gemini_cover_information(
        config["book_title"], introduction,
        research={"mode": "internal_knowledge_fallback", "sources": []},
    )
    _write_json(cover_dir / "cover_information_draft.json", draft)


def _run_review_stage(config):
    _, cover_dir = _stage_paths(config)
    introduction_record = _read_json(cover_dir / "book_introduction.json")
    draft = _read_json(cover_dir / "cover_information_draft.json")
    if introduction_record.get("manual_cover"):
        return

    introduction = str(introduction_record.get("introduction") or "")
    research = {"mode": "internal_knowledge_fallback", "sources": []}
    reviewed = review_cover_information(
        config["book_title"], introduction, research, draft,
    )
    brief = {
        "analysis_method": "local_evidence_only",
        "source": "Gemini book introduction",
        "synopsis": introduction,
        "analysis": reviewed["analysis"],
        "story_facts": reviewed["story_facts"],
        "visual_brief": reviewed["visual_brief"],
        "template_version": reviewed["template_version"],
        "prompt": reviewed["prompt"],
    }
    _write_json(
        cover_dir / "master_cover_prompt.json",
        {
            "book_title": config["book_title"],
            "template_version": COVER_TEMPLATE_VERSION,
            "brief": brief,
            "prompt": reviewed["prompt"],
        },
    )


def _write_manifest(config, args, cover_path: Path, prompt_path: Path, cover_dir: Path):
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
    _write_json(cover_dir / "cover-preflight-manifest.json", manifest)


def _run_cover_stage(config, args):
    _, cover_dir = _stage_paths(config)
    introduction_record = _read_json(cover_dir / "book_introduction.json")
    prompt_path = cover_dir / "master_cover_prompt.json"
    prompt_record = _read_json(prompt_path)
    cover_path = cover_dir / "master_cover.jpg"

    if not introduction_record.get("manual_cover"):
        prompt = str(prompt_record.get("prompt") or "")
        if not prompt:
            raise RuntimeError("HF 生圖 Prompt 為空")
        master = download_ai_image(prompt, width=1280, height=720)
        temporary = cover_path.with_suffix(".jpg.tmp")
        master.convert("RGB").save(temporary, "JPEG", quality=98, subsampling=0, optimize=False)
        temporary.replace(cover_path)
        introduction = str(introduction_record.get("introduction") or "")
        save_process_log(str(cover_dir), config["book_title"], introduction, introduction, prompt)

    if not cover_path.is_file():
        raise RuntimeError("封面圖不存在")
    _write_manifest(config, args, cover_path, prompt_path, cover_dir)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--restore")
    parser.add_argument(
        "--stage",
        choices=("introduction", "cover-information", "review", "cover"),
    )
    parser.add_argument("--book-title")
    parser.add_argument("--catalog-url")
    parser.add_argument("--book-profile-snapshot-b64", default="")
    args = parser.parse_args()
    config_path = Path(args.config)
    if args.stage == "introduction":
        if not args.book_title or not args.catalog_url:
            parser.error("introduction 階段需要 --book-title 與 --catalog-url")
        _run_introduction_stage(args, config_path)
        return

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
    if args.stage == "cover-information":
        _run_cover_information_stage(config)
    elif args.stage == "review":
        _run_review_stage(config)
    elif args.stage == "cover":
        _run_cover_stage(config, args)
    else:
        parser.error("產生封面時必須指定 --stage")


if __name__ == "__main__":
    main()
