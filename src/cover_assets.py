"""Manual cover validation, local caching, and Hugging Face persistence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image, ImageOps

SIZE = (1280, 720)
MAX_INPUT_PIXELS = 80_000_000
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_path(project_root, profile_id):
    return Path(project_root) / "Workspace" / ".cover-cache" / profile_id / "master_cover.jpg"


def normalize_manual_cover(source, destination):
    """Decode, orient, center-crop to 16:9, and write a verified YouTube-safe JPEG."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise ValueError("找不到選取的圖片檔案")
    with Image.open(source) as opened:
        opened.verify()
    with Image.open(source) as opened:
        if opened.width * opened.height > MAX_INPUT_PIXELS:
            raise ValueError("圖片像素過大（上限 8,000 萬像素）")
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image = ImageOps.fit(image, SIZE, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    destination.parent.mkdir(parents=True, exist_ok=True)
    quality = 94
    while True:
        image.save(destination, "JPEG", quality=quality, optimize=True)
        if destination.stat().st_size < MAX_OUTPUT_BYTES:
            break
        quality -= 4
        if quality < 50:
            raise ValueError("圖片無法壓縮至 YouTube 2 MB 限制內")
    validate_cached_cover(destination)
    return {
        "width": SIZE[0], "height": SIZE[1], "bytes": destination.stat().st_size,
        "sha256": sha256_file(destination), "quality": quality,
    }


def validate_cached_cover(path, expected_sha256=""):
    path = Path(path)
    if not path.is_file() or path.stat().st_size >= MAX_OUTPUT_BYTES:
        raise ValueError("手動封面不存在或超過 2 MB")
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        if image.size != SIZE or image.format != "JPEG":
            raise ValueError("手動封面必須是 1280×720 JPEG")
    actual = sha256_file(path)
    if expected_sha256 and actual != expected_sha256:
        raise ValueError("手動封面 SHA-256 校驗失敗")
    return actual


def default_repo(token, configured=""):
    if configured:
        return configured.strip()
    from huggingface_hub import HfApi
    return f"{HfApi(token=token).whoami()['name']}/audiobook-archive"


def upload_cover(local_path, profile_id, token, repo_id=""):
    from huggingface_hub import HfApi
    repo_id = default_repo(token, repo_id)
    remote = f"manual-covers/{profile_id}/master_cover.jpg"
    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
    api.upload_file(path_or_fileobj=str(local_path), path_in_repo=remote, repo_id=repo_id,
                    repo_type="dataset", commit_message=f"Update manual cover {profile_id}")
    return repo_id, remote


def restore_cover(record, destination, token):
    from huggingface_hub import hf_hub_download
    cached = hf_hub_download(record["repo_id"], record["remote_path"], repo_type="dataset", token=token)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_bytes(Path(cached).read_bytes())
    validate_cached_cover(destination, record.get("sha256", ""))
    return destination


def restore_from_config(config_path, workspace_root="Workspace"):
    import yaml
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    record = config.get("manual_cover") or {}
    if not record:
        return False
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("手動封面已設定，但缺少 HF_TOKEN")
    destination = Path(workspace_root) / config["book_title"] / "Cover" / "master_cover.jpg"
    restore_cover(record, destination, token)
    print(f"[MANUAL_COVER] restored {destination}", flush=True)
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-config", required=True)
    parser.add_argument("--workspace-root", default="Workspace")
    args = parser.parse_args()
    restore_from_config(args.restore_config, args.workspace_root)
