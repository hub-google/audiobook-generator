"""Manual cover validation, local caching, and Hugging Face persistence."""

from __future__ import annotations

import hashlib
import base64
import json
import logging
import os
import time
from pathlib import Path

import requests

from PIL import Image, ImageOps

SIZE = (1280, 720)
MAX_INPUT_PIXELS = 80_000_000


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cache_path(project_root, profile_id):
    return Path(project_root) / "Workspace" / ".cover-cache" / profile_id / "master_cover.jpg"


def normalize_manual_cover(source, destination):
    """Decode, orient, center-crop to 16:9, and write one high-quality JPEG."""
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
    quality = 98
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    image.save(temporary, "JPEG", quality=quality, subsampling=0, optimize=False)
    os.replace(temporary, destination)
    validate_cached_cover(destination)
    digest = sha256_file(destination)
    marker = destination.with_suffix(".manual.json")
    temporary_marker = marker.with_suffix(marker.suffix + ".tmp")
    temporary_marker.write_text(json.dumps({"source": "manual", "sha256": digest}), encoding="utf-8")
    os.replace(temporary_marker, marker)
    return {
        "width": SIZE[0], "height": SIZE[1], "bytes": destination.stat().st_size,
        "sha256": digest, "quality": quality,
    }


def validate_cached_cover(path, expected_sha256=""):
    path = Path(path)
    if not path.is_file() or path.stat().st_size < 10_000:
        raise ValueError("手動封面不存在或檔案異常過小")
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
    from huggingface_hub.errors import HfHubHTTPError
    repo_id = default_repo(token, repo_id)
    remote = f"manual-covers/{profile_id}/master_cover.jpg"
    api = HfApi(token=token)
    try:
        api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)
        api.upload_file(path_or_fileobj=str(local_path), path_in_repo=remote, repo_id=repo_id,
                        repo_type="dataset", commit_message=f"Update manual cover {profile_id}")
    except HfHubHTTPError as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in {401, 403}:
            raise RuntimeError(
                "Hugging Face 拒絕上傳：HF_TOKEN 沒有寫入權限。\n\n"
                "請到 Hugging Face → Settings → Access Tokens 建立 Write token；"
                "若使用 Fine-grained token，必須授予資料庫 "
                f"{repo_id} 的寫入權限。然後把新 token 更新到本機 .env 的 HF_TOKEN，重開程式後再試。"
            ) from error
        raise RuntimeError(f"Hugging Face 上傳失敗：{error}") from error
    return repo_id, remote


def upload_github_cover(local_path, profile_id, repo, token, branch="automation-state"):
    """Persist a manual cover beside the durable book profiles on GitHub."""
    remote = f"manual-covers/{profile_id}/master_cover.jpg"
    url = f"https://api.github.com/repos/{repo}/contents/{remote}"
    headers = {
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    current = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
    if current.status_code not in {200, 404}:
        raise RuntimeError(f"GitHub 手動封面檢查失敗 ({current.status_code})：{current.text}")
    body = {
        "message": f"Update manual cover {profile_id}", "branch": branch,
        "content": base64.b64encode(Path(local_path).read_bytes()).decode("ascii"),
    }
    if current.status_code == 200:
        body["sha"] = current.json()["sha"]
    response = requests.put(url, headers=headers, json=body, timeout=60)
    if response.status_code not in {200, 201}:
        raise RuntimeError(f"GitHub 手動封面上傳失敗 ({response.status_code})：{response.text}")
    return {
        "provider": "github", "repo": repo, "branch": branch, "remote_path": remote,
        "blob_sha": response.json()["content"]["sha"],
    }


def restore_cover(record, destination, token):
    last_error = None
    for attempt in range(1, 4):
        try:
            if record.get("provider") == "github":
                github_token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
                repo = record.get("repo") or os.environ.get("GITHUB_REPOSITORY", "")
                branch = record.get("branch") or "automation-state"
                url = f"https://api.github.com/repos/{repo}/contents/{record['remote_path']}"
                response = requests.get(url, headers={
                    "Accept": "application/vnd.github.raw+json", "Authorization": f"Bearer {github_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                }, params={"ref": branch}, timeout=60)
                if response.status_code != 200:
                    raise RuntimeError(f"GitHub 手動封面下載失敗 ({response.status_code})")
                cached = Path(destination).with_suffix(".download")
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(response.content)
            else:
                from huggingface_hub import hf_hub_download
                cached = hf_hub_download(
                    record["repo_id"], record["remote_path"],
                    repo_type="dataset", token=token, force_download=(attempt > 1),
                )
            destination = Path(destination)
            validate_cached_cover(cached, record.get("sha256", ""))
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_destination = destination.with_suffix(destination.suffix + ".tmp")
            temporary_destination.write_bytes(Path(cached).read_bytes())
            os.replace(temporary_destination, destination)
            marker = destination.with_suffix(".manual.json")
            temporary_marker = marker.with_suffix(marker.suffix + ".tmp")
            temporary_marker.write_text(json.dumps({"source": "manual", "sha256": record.get("sha256", "")}), encoding="utf-8")
            os.replace(temporary_marker, marker)
            return destination
        except Exception as err:
            last_error = err
            logging.warning("restore_cover attempt %s/3 failed: %s", attempt, err)
            if attempt < 3:
                time.sleep(5 * attempt)
    raise RuntimeError(f"restore_cover 失敗 (已重試 3 次): {last_error}")


def restore_from_config(config_path, workspace_root="Workspace"):
    import yaml
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    record = config.get("manual_cover") or {}
    if not record and config.get("catalog_url") and os.environ.get("GITHUB_REPOSITORY"):
        # Retry runs can recover worker artifacts from an older immutable source
        # config.  The durable per-book profile remains authoritative for a
        # user-uploaded cover and must be checked before starting paid AI image
        # generation.
        try:
            try:
                from .book_profiles import GitHubBookProfileStore, get_book_profile
            except ImportError:
                from book_profiles import GitHubBookProfileStore, get_book_profile
            profile_token = os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
            if profile_token:
                profiles, _ = GitHubBookProfileStore(
                    os.environ["GITHUB_REPOSITORY"], profile_token,
                ).load()
                profile_id, profile = get_book_profile(
                    profiles, config["catalog_url"], config.get("book_title", ""),
                )
                configured_id = str(config.get("book_profile_id") or "")
                if configured_id and configured_id != profile_id:
                    raise RuntimeError(
                        f"封面 profile 不一致：config={configured_id}, current={profile_id}"
                    )
                record = profile.get("manual_cover") or {}
                if record:
                    logging.info(
                        "[MANUAL_COVER_RECOVERY] locked config omitted manual_cover; "
                        f"recovered profile {profile_id}"
                    )
        except Exception as error:
            logging.warning(f"無法查詢最新手動封面設定：{error}")
    if not record:
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("- [ ] 手動封面：未設定，將使用 Gemini／HF 自動封面流程\n")
        return False
    token = (
        os.environ.get("GH_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
        if record.get("provider") == "github" else os.environ.get("HF_TOKEN", "")
    )
    if not token:
        provider = "GitHub" if record.get("provider") == "github" else "Hugging Face"
        raise RuntimeError(f"手動封面已設定，但缺少 {provider} 讀取 Token")
    try:
        from .source_identity import workspace_name
    except ImportError:
        from source_identity import workspace_name
    destination = Path(workspace_root) / workspace_name(config) / "Cover" / "master_cover.jpg"
    restore_cover(record, destination, token)
    print(f"[MANUAL_COVER_CHECK] PASS | restored and verified {destination}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("- [x] 手動封面：已從雲端還原並通過尺寸、格式與 SHA-256 檢核；跳過 Gemini／HF 生圖\n")
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-config", required=True)
    parser.add_argument("--workspace-root", default="Workspace")
    args = parser.parse_args()
    restore_from_config(args.restore_config, args.workspace_root)
