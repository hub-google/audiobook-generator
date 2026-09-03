"""Global, downstream-first planner for cross-run audiobook resume.

The planner treats manifests plus their referenced bytes as the source of truth;
job conclusions are deliberately ignored.  It downloads artifacts by ID from
the paginated Actions API so runs with more than 100 artifacts remain resumable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import yaml

try:
    from .artifact_validation import ArtifactValidationError, validate_srt, validate_worker_manifest
    from .publication_checkpoint import PART_STEPS, plan_fingerprint
except ImportError:
    from artifact_validation import ArtifactValidationError, validate_srt, validate_worker_manifest
    from publication_checkpoint import PART_STEPS, plan_fingerprint


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_fingerprint(config):
    """Identity used to reject artifacts belonging to a different book build."""
    payload = {
        "book_profile_id": config.get("book_profile_id"),
        "profile_revision": config.get("profile_revision"),
        "selected_indices": [int(x) for x in config.get("selected_indices") or []],
        "chapter_order": config.get("chapter_order") or [],
        "cleaner_fingerprint": (config.get("cleaner") or {}).get("fingerprint"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def list_run_artifacts(repo, run_id, runner=subprocess.run):
    """Return every unexpired artifact in a run (not merely API page one)."""
    command = [
        "gh", "api", "--paginate", "--slurp",
        f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
    ]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"cannot list artifacts for Run {run_id}: {result.stderr.strip()}")
    pages = json.loads(result.stdout or "[]")
    if isinstance(pages, dict):
        pages = [pages]
    artifacts = []
    for page in pages:
        artifacts.extend(x for x in page.get("artifacts", []) if not x.get("expired"))
    # A rerun can leave duplicate names. The newest artifact is authoritative.
    by_name = {}
    for item in sorted(artifacts, key=lambda x: int(x.get("id") or 0)):
        by_name[item["name"]] = item
    return by_name


def list_candidate_runs(repo, current_run_id, explicit="", runner=subprocess.run):
    if explicit:
        return [str(explicit)]
    result = runner([
        "gh", "api", "--paginate", "--slurp",
        f"repos/{repo}/actions/workflows/audiobook.yml/runs?event=workflow_dispatch&per_page=100",
    ], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"cannot discover resumable runs: {result.stderr.strip()}")
    pages = json.loads(result.stdout or "[]")
    if isinstance(pages, dict):
        pages = [pages]
    runs = [r for p in pages for r in p.get("workflow_runs", [])]
    return [str(r["id"]) for r in runs if str(r["id"]) != str(current_run_id)]


def download_artifact(repo, artifact, destination, runner=subprocess.run):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "artifact.zip"
    result = runner([
        "gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"cannot download artifact {artifact['name']}: {result.stderr!r}")
    archive.write_bytes(result.stdout)
    with zipfile.ZipFile(archive) as bundle:
        root = destination.resolve()
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"unsafe artifact member: {member.filename}")
        bundle.extractall(destination)
    archive.unlink()
    return destination


def _find(root, name):
    matches = list(Path(root).glob(f"**/{name}"))
    return matches[0] if len(matches) == 1 else None


def validate_plan(root, config):
    path = _find(root, "parts-plan.json")
    saved_config = _find(root, "config.yaml")
    if not path or not saved_config:
        raise ArtifactValidationError("prepared-plan lacks parts-plan.json or config.yaml")
    previous = yaml.safe_load(saved_config.read_text(encoding="utf-8")) or {}
    if config_fingerprint(previous) != config_fingerprint(config):
        raise ArtifactValidationError("prepared-plan config fingerprint mismatch")
    plan = json.loads(path.read_text(encoding="utf-8"))
    selected = [int(x) for x in config.get("selected_indices") or []]
    if [int(x) for x in plan.get("selected_indices") or []] != selected:
        raise ArtifactValidationError("prepared-plan input range mismatch")
    if not plan.get("parts") or not plan.get("source_run_id"):
        raise ArtifactValidationError("prepared-plan is incomplete")
    plan["plan_fingerprint"] = plan_fingerprint(plan["parts"])
    return plan


def validate_merge_shard(root, plan, expected_parts=None):
    recovered = {}
    for path in Path(root).glob("**/shard-manifest-*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if str(data.get("source_run_id")) != str(plan["source_run_id"]):
            raise ArtifactValidationError("merge shard source_run_id mismatch")
        if int(data.get("schema_version") or 1) >= 2:
            if data.get("status") != "completed" or data.get("fingerprint") != plan_fingerprint(plan["parts"]):
                raise ArtifactValidationError("merge shard status/fingerprint mismatch")
        for part in data.get("parts") or []:
            number = int(part["part_num"])
            expected = next((p for p in plan["parts"] if int(p["part_num"]) == number), None)
            if not expected or [int(x) for x in part.get("chapters") or []] != [int(x) for x in expected["chapters"]]:
                raise ArtifactValidationError(f"Part {number} input range mismatch")
            subtitle = _find(root, part["subtitle"])
            if not subtitle or subtitle.stat().st_size != int(part["subtitle_bytes"]):
                raise ArtifactValidationError(f"Part {number} subtitle size mismatch")
            if _sha256(subtitle) != part["subtitle_sha256"]:
                raise ArtifactValidationError(f"Part {number} subtitle hash mismatch")
            validate_srt(str(subtitle), float(part["duration"]))
            if not part.get("video_sha256") or int(part.get("video_bytes") or 0) <= 0 or not part.get("hf_video_path"):
                raise ArtifactValidationError(f"Part {number} video evidence incomplete")
            recovered[number] = part
    wanted = {int(x) for x in (expected_parts or [])}
    if wanted and not wanted.issubset(recovered):
        raise ArtifactValidationError(f"merge shard missing Parts {sorted(wanted - set(recovered))}")
    return recovered


def validate_final_merge(root, plan):
    manifest_path = _find(root, "final-merge-manifest.json")
    if not manifest_path:
        raise ArtifactValidationError("final merge manifest missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_fp = plan_fingerprint(plan["parts"])
    if manifest.get("status") != "completed" or manifest.get("fingerprint") != expected_fp:
        raise ArtifactValidationError("final merge status/fingerprint mismatch")
    if str(manifest.get("source_run_id")) != str(plan["source_run_id"]):
        raise ArtifactValidationError("final merge source_run_id mismatch")
    expected = {int(p["part_num"]) for p in plan["parts"]}
    recovered = validate_merge_shard(root, plan, expected)
    if set(recovered) != expected:
        raise ArtifactValidationError("final merge Part set is incomplete")
    return manifest


def validate_publication(root, plan, config, require_complete=False):
    state_path = _find(root, "state.json")
    ledger_path = _find(root, "part_execution.json")
    if not state_path or not ledger_path:
        raise ArtifactValidationError("publication checkpoint lacks state or execution ledger")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    expected_fp = plan_fingerprint(plan["parts"])
    actual_fp = ledger.get("plan_fingerprint") or plan_fingerprint(ledger.get("plan") or [])
    if actual_fp != expected_fp or (state.get("plan_fingerprint") and state["plan_fingerprint"] != expected_fp):
        raise ArtifactValidationError("publication plan fingerprint mismatch")
    profile = str(config.get("book_profile_id") or "")
    if profile and ledger.get("book_profile_id") and str(ledger["book_profile_id"]) != profile:
        raise ArtifactValidationError("publication book fingerprint mismatch")
    parts = sorted(plan["parts"], key=lambda x: int(x["part_num"]))
    for position, part in enumerate(parts):
        record = (ledger.get("parts") or {}).get(str(int(part["part_num"])), {})
        playlist = record.get("playlist") or {}
        if playlist.get("status") == "completed" and int(playlist.get("position", -1)) != position:
            raise ArtifactValidationError("publication playlist order is not globally stable")
    if require_complete:
        if state.get("status") != "complete":
            raise ArtifactValidationError("publication is not complete")
        for part in parts:
            record = (ledger.get("parts") or {}).get(str(int(part["part_num"])), {})
            steps = record.get("steps") or {}
            if any((steps.get(step) or {}).get("status") != "completed" for step in PART_STEPS):
                raise ArtifactValidationError(f"publication Part {part['part_num']} is incomplete")
        for key in ("playlist", "final_book_validation"):
            if ((ledger.get("global_steps") or {}).get(key) or {}).get("status") != "completed":
                raise ArtifactValidationError(f"publication global {key} is incomplete")
    return state, ledger


def _worker_expected(matrix_item, config):
    selected = [int(x) for x in config.get("selected_indices") or []]
    start, end = int(matrix_item["start_chap"]), int(matrix_item["end_chap"])
    return [x for x in selected if start <= x <= end]


def validate_worker(root, matrix_item, config, require_complete=False):
    worker_id = int(matrix_item["worker_id"])
    manifest = _find(root, f"manifest-worker-{worker_id}.json")
    if not manifest:
        return {"complete": False, "completed_chapters": []}
    data = json.loads(manifest.read_text(encoding="utf-8"))
    expected = _worker_expected(matrix_item, config)
    completed = []
    for item in data.get("chapters") or []:
        number = int(item["chap_num"])
        video = _find(root, Path(item.get("video_relpath") or item.get("video") or f"chapter_{number}.mp4").name)
        subtitle = _find(root, Path(item.get("srt_relpath") or item.get("subtitle") or f"chapter_{number}.srt").name)
        valid = bool(video and subtitle and video.stat().st_size > 1000 and subtitle.stat().st_size > 10)
        if valid and item.get("video_sha256"):
            valid = video.stat().st_size == int(item.get("video_bytes") or 0) and _sha256(video) == item["video_sha256"]
        if valid and item.get("srt_sha256"):
            valid = subtitle.stat().st_size == int(item.get("srt_bytes") or 0) and _sha256(subtitle) == item["srt_sha256"]
        if valid:
            completed.append(number)
    complete = set(expected) == set(completed) | {int(x) for x in data.get("source_missing") or []}
    if require_complete and complete:
        validate_worker_manifest(str(manifest), worker_id, expected, data.get("source_missing") or [])
    return {"complete": complete, "completed_chapters": sorted(completed)}


def build_final_manifest(plan_path, sidecar_dir, output_dir, source_run_id):
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for path in Path(sidecar_dir).glob("**/*"):
        if path.is_file():
            shutil.copy2(path, output / path.name)
    parts = validate_merge_shard(output, plan, [int(p["part_num"]) for p in plan["parts"]])
    manifest = {
        "schema_version": 1, "stage": "final_merge", "status": "completed",
        "fingerprint": plan_fingerprint(plan["parts"]),
        "source_run_id": str(plan.get("source_run_id") or source_run_id),
        "execution_run_id": str(source_run_id),
        "input_parts": sorted(parts),
        "outputs": [{"part_num": n, "video": parts[n]["video"],
                     "sha256": parts[n]["video_sha256"], "bytes": parts[n]["video_bytes"],
                     "duration": parts[n]["duration"]} for n in sorted(parts)],
    }
    (output / "final-merge-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _copy_tree(source, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for path in Path(source).glob("**/*"):
        if path.is_file():
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def plan_resume(repo, current_run_id, config_path, matrix_path, output_dir, explicit_source=""):
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    worker_matrix = (json.loads(Path(matrix_path).read_text(encoding="utf-8")) or {}).get("include") or []
    output = Path(output_dir); output.mkdir(parents=True, exist_ok=True)
    empty = {"include": []}
    result = {"mode": "workers", "source_run_id": "", "worker_matrix": {"include": worker_matrix},
              "merge_matrix": empty, "publication_complete": False, "has_plan": False,
              "final_merge_ready": False}

    for run_id in list_candidate_runs(repo, current_run_id, explicit_source):
        try:
            artifacts = list_run_artifacts(repo, run_id)
            if "shared-config" not in artifacts:
                continue
            with tempfile.TemporaryDirectory() as temporary:
                shared = download_artifact(repo, artifacts["shared-config"], Path(temporary) / "shared")
                source_config_path = _find(shared, "config.yaml")
                if not source_config_path:
                    continue
                source_config = yaml.safe_load(source_config_path.read_text(encoding="utf-8")) or {}
                if config_fingerprint(source_config) != config_fingerprint(config):
                    if explicit_source:
                        raise ArtifactValidationError("explicit source Run config fingerprint mismatch")
                    continue
            result["source_run_id"] = run_id
            result["artifacts"] = artifacts
            break
        except Exception:
            if explicit_source:
                raise
    else:
        _write_plan(result, output)
        return result

    run_id, artifacts = result["source_run_id"], result.pop("artifacts")
    plan = None
    if "prepared-plan" in artifacts:
        try:
            root = download_artifact(repo, artifacts["prepared-plan"], output / "prepared-plan")
            plan = validate_plan(root, config)
            result["has_plan"] = True
        except Exception as error:
            result.setdefault("rejected", []).append(f"prepared-plan: {error}")
            shutil.rmtree(output / "prepared-plan", ignore_errors=True)

    # 1. Publication, then Final Merge. Never inspect upstream after success.
    if plan and "youtube-upload-checkpoint" in artifacts:
        try:
            root = download_artifact(repo, artifacts["youtube-upload-checkpoint"], output / "publication")
            validate_publication(root, plan, config, require_complete=True)
            result.update(mode="complete", publication_complete=True, worker_matrix=empty, merge_matrix=empty,
                          final_merge_ready=True)
            _write_plan(result, output); return result
        except Exception as complete_error:
            try:
                validate_publication(output / "publication", plan, config, require_complete=False)
            except Exception as error:
                result.setdefault("rejected", []).append(f"publication: {error}")
                shutil.rmtree(output / "publication", ignore_errors=True)
            else:
                result.setdefault("notes", []).append(f"publication checkpoint is resumable: {complete_error}")

    if plan and "final-merge" in artifacts:
        try:
            root = download_artifact(repo, artifacts["final-merge"], output / "final-merge")
            validate_final_merge(root, plan)
            result.update(mode="publication", worker_matrix=empty, merge_matrix=empty, final_merge_ready=True)
            _write_plan(result, output); return result
        except Exception as error:
            result.setdefault("rejected", []).append(f"final-merge: {error}")
            shutil.rmtree(output / "final-merge", ignore_errors=True)

    # 2. Reuse every valid merge shard and schedule only missing Parts.
    recovered_parts = {}
    if plan:
        merge_root = output / "merge-results"
        for name, artifact in artifacts.items():
            if not name.startswith("merge-result-"):
                continue
            try:
                shard_root = download_artifact(repo, artifact, merge_root / name)
                recovered_parts.update(validate_merge_shard(shard_root, plan))
            except Exception as error:
                result.setdefault("rejected", []).append(f"{name}: {error}")
                shutil.rmtree(merge_root / name, ignore_errors=True)
        all_parts = {int(p["part_num"]) for p in plan["parts"]}
        missing_parts = sorted(all_parts - set(recovered_parts))
        if not missing_parts:
            result.update(mode="final_merge", worker_matrix=empty, merge_matrix=empty)
            _write_plan(result, output); return result
        assignments = []
        for index, number in enumerate(missing_parts):
            assignments.append({"merge_worker_id": index, "part_numbers": str(number)})
        result["merge_matrix"] = {"include": assignments}

    # 3. Only now inspect Workers. Completed workers are omitted; partial ones
    # carry their validated workspace in the resume bundle.
    needed_chapters = set()
    if plan and result["merge_matrix"]["include"]:
        missing_part_numbers = {int(x["part_numbers"]) for x in result["merge_matrix"]["include"]}
        needed_chapters = {int(c) for p in plan["parts"] if int(p["part_num"]) in missing_part_numbers for c in p["chapters"]}
    scheduled = []
    for item in worker_matrix:
        expected = set(_worker_expected(item, config))
        if needed_chapters and not expected.intersection(needed_chapters):
            continue
        name = f"video-worker-{int(item['worker_id'])}"
        if name not in artifacts:
            scheduled.append(item); continue
        try:
            root = download_artifact(repo, artifacts[name], output / "workers" / name)
            status = validate_worker(root, item, config, require_complete=True)
            if not status["complete"]:
                resumed = dict(item); resumed["resume"] = True
                scheduled.append(resumed)
        except Exception as error:
            result.setdefault("rejected", []).append(f"{name}: {error}")
            scheduled.append(item)
    result["worker_matrix"] = {"include": scheduled}
    result["mode"] = "merge" if plan else "workers"
    _write_plan(result, output)
    return result


def _write_plan(result, output):
    serializable = dict(result)
    path = Path(output) / "resume-plan.json"
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _emit_outputs(result, path):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key in ("mode", "source_run_id", "publication_complete", "has_plan", "final_merge_ready"):
            value = result.get(key, "")
            handle.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
        handle.write("worker_matrix=" + json.dumps(result["worker_matrix"], separators=(",", ":")) + "\n")
        handle.write("merge_matrix=" + json.dumps(result["merge_matrix"], separators=(",", ":")) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo")
    parser.add_argument("--run-id")
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--config")
    parser.add_argument("--matrix")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--github-output", default="")
    parser.add_argument("--build-final", action="store_true")
    parser.add_argument("--plan")
    parser.add_argument("--sidecar-dir")
    args = parser.parse_args()
    if args.build_final:
        build_final_manifest(args.plan, args.sidecar_dir, args.output_dir, args.run_id)
        return
    result = plan_resume(args.repo, args.run_id, args.config, args.matrix, args.output_dir, args.source_run_id)
    _emit_outputs(result, args.github_output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
