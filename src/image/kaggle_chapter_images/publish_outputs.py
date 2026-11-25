from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def add(local: Path, remote: str) -> CommitOperationAdd:
    if not local.is_file():
        raise FileNotFoundError(local)
    return CommitOperationAdd(path_in_repo=remote, path_or_fileobj=str(local))


def publish(output: Path, assets: Path, repo_id: str, env_file: Path) -> dict:
    load_env(env_file)
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    report = json.loads((output / "run_report.json").read_text(encoding="utf-8"))
    version_root = "chapter_images/v1"
    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", private=True, exist_ok=True)

    content_ops: list[CommitOperationAdd] = []
    for image in sorted(output.glob("chapter_*/scene_*.webp")):
        relative = image.relative_to(output).as_posix()
        content_ops.append(add(image, f"{version_root}/images/{relative}"))
    for board in sorted(output.glob("storyboard_*.json")):
        chapter = board.stem.split("_")[-1]
        content_ops.append(add(board, f"{version_root}/storyboards/chapter_{chapter}.json"))
    content_ops += [add(assets / "characters.json", f"{version_root}/characters/characters.json"),
                    add(assets / "style_bible.json", f"{version_root}/style_bible.json")]
    content_commit = api.create_commit(repo_id, repo_type="dataset", operations=content_ops,
                                       commit_message=f"Upload Kaggle image content {report['run_id']}").oid

    manifest_ops = [add(output / "manifests" / name, f"{version_root}/manifests/{name}")
                    for name in ("chapters.jsonl", "images.jsonl")]
    manifest_ops.append(add(output / "run_report.json", f"{version_root}/manifests/run_{report['run_id']}.json"))
    manifest_commit = api.create_commit(repo_id, repo_type="dataset", operations=manifest_ops,
                                        commit_message=f"Publish manifests {report['run_id']}").oid
    latest = output / "latest.json"
    latest_data = json.loads(latest.read_text(encoding="utf-8"))
    latest_data.update({"content_commit": content_commit, "manifest_commit": manifest_commit})
    latest.write_text(json.dumps(latest_data, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_commit = api.create_commit(repo_id, repo_type="dataset",
                                      operations=[add(latest, "chapter_images/latest.json")],
                                      commit_message=f"Advance latest pointer {report['run_id']}").oid
    return {"repo_id": repo_id, "content_commit": content_commit,
            "manifest_commit": manifest_commit, "latest_commit": latest_commit}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assets", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--repo-id", default="hub-google/quanzhigaoshou-chapter-images")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.output, args.assets, args.repo_id, args.env_file), indent=2))


if __name__ == "__main__":
    main()
