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
    from .artifact_validation import ArtifactValidationError, validate_srt, validate_video, validate_worker_manifest
    from .part_matrix import build_merge_matrix
    from .publication_checkpoint import PART_STEPS, plan_fingerprint
except ImportError:
    from artifact_validation import ArtifactValidationError, validate_srt, validate_video, validate_worker_manifest
    from part_matrix import build_merge_matrix
    from publication_checkpoint import PART_STEPS, plan_fingerprint


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gh_executable():
    configured = os.environ.get("GH_CLI", "").strip()
    if configured:
        return configured
    found = shutil.which("gh")
    if found:
        return found
    windows = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    return str(windows) if windows.is_file() else "gh"


def config_fingerprint(config):
    """Identity used to reject artifacts belonging to a different book build."""
    payload = {
        "book_title": config.get("book_title"),
        "book_profile_id": config.get("book_profile_id"),
        "profile_revision": config.get("profile_revision"),
        "catalog_url": config.get("catalog_url"),
        "selected_indices": [int(x) for x in config.get("selected_indices") or []],
        "source_indices": [int(x) for x in config.get("source_indices") or []],
        "chapters": config.get("chapters") or [],
        "chapter_titles": config.get("chapter_titles") or [],
        "chapter_order": config.get("chapter_order") or [],
        "renumber_selected": bool(config.get("renumber_selected")),
        "cleaner_fingerprint": (config.get("cleaner") or {}).get("fingerprint"),
        "tts": config.get("tts") or {},
        "video": config.get("video") or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def list_run_artifacts(repo, run_id, runner=subprocess.run):
    """Return every unexpired artifact in a run (not merely API page one)."""
    command = [
        _gh_executable(), "api", "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100",
        "--jq", ".artifacts[] | @json",
    ]
    result = runner(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"cannot list artifacts for Run {run_id}: {result.stderr.strip()}")
    artifacts = [json.loads(line) for line in (result.stdout or "").splitlines() if line.strip()]
    artifacts = [x for x in artifacts if not x.get("expired")]
    # A rerun can leave duplicate names. The newest artifact is authoritative.
    by_name = {}
    for item in sorted(artifacts, key=lambda x: int(x.get("id") or 0)):
        by_name[item["name"]] = item
    return by_name


def list_candidate_runs(repo, current_run_id, explicit="", runner=subprocess.run):
    if explicit:
        return [str(explicit)]
    result = runner([
        _gh_executable(), "api", "--paginate",
        f"repos/{repo}/actions/workflows/audiobook.yml/runs?event=workflow_dispatch&per_page=100",
        "--jq", ".workflow_runs[].id",
    ], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"cannot discover resumable runs: {result.stderr.strip()}")
    runs = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return [run_id for run_id in runs if run_id != str(current_run_id)]


def download_artifact(repo, artifact, destination, runner=subprocess.run):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "artifact.zip"
    result = runner([
        _gh_executable(), "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip",
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


def _find_manifest_path(root, relative_path, fallback_name=""):
    root = Path(root).resolve()
    value = str(relative_path or "").replace("\\", "/").lstrip("/")
    if value:
        candidate = (root / value).resolve()
        if root in candidate.parents and candidate.is_file():
            return candidate
        matches = [p for p in root.glob(f"**/{value}") if p.is_file()]
        if len(matches) > 1:
            raise ArtifactValidationError(f"ambiguous artifact relative path: {value}")
        if matches:
            return matches[0]
    if fallback_name:
        matches = [p for p in root.glob(f"**/{fallback_name}") if p.is_file()]
        if len(matches) > 1:
            raise ArtifactValidationError(f"ambiguous legacy artifact basename: {fallback_name}")
        return matches[0] if matches else None
    return None


class TransientRemoteValidationError(RuntimeError):
    """Remote evidence could not be checked; retry instead of invalidating it."""


def verify_hf_video(part, *, token=None, repo_id=None):
    token = token if token is not None else os.environ.get("HF_TOKEN", "")
    repo_id = repo_id if repo_id is not None else os.environ.get("HF_ARCHIVE_REPO", "").strip()
    if not token:
        raise TransientRemoteValidationError("HF_TOKEN is required to validate merge bytes")
    if not repo_id:
        try:
            from huggingface_hub import HfApi
            repo_id = f"{HfApi(token=token).whoami()['name']}/audiobook-archive"
        except Exception as error:
            raise TransientRemoteValidationError(f"cannot resolve HF archive repository: {error}") from error
    try:
        from huggingface_hub import hf_hub_download
        path = Path(hf_hub_download(repo_id=repo_id, filename=part["hf_video_path"], token=token))
    except Exception as error:
        try:
            from huggingface_hub.errors import EntryNotFoundError, RemoteEntryNotFoundError
            missing_types = (EntryNotFoundError, RemoteEntryNotFoundError)
        except ImportError:
            missing_types = ()
        if missing_types and isinstance(error, missing_types):
            raise ArtifactValidationError(f"HF video is missing: {part.get('hf_video_path')}") from error
        raise TransientRemoteValidationError(
            f"cannot verify HF video {part.get('hf_video_path')}: {error}"
        ) from error
    if path.stat().st_size != int(part.get("video_bytes") or 0):
        raise ArtifactValidationError(f"HF video size mismatch: {part.get('hf_video_path')}")
    if _sha256(path) != part.get("video_sha256"):
        raise ArtifactValidationError(f"HF video hash mismatch: {part.get('hf_video_path')}")
    return path


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


def validate_merge_shard(root, plan, expected_parts=None, remote_verifier=verify_hf_video):
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
            remote_verifier(part)
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
        video = _find_manifest_path(root, item.get("video_relpath") or item.get("video"), f"chapter_{number}.mp4")
        subtitle = _find_manifest_path(root, item.get("srt_relpath") or item.get("subtitle"), f"chapter_{number}.srt")
        valid = bool(video and subtitle and video.stat().st_size > 1000 and subtitle.stat().st_size > 10)
        if valid and item.get("video_sha256"):
            valid = video.stat().st_size == int(item.get("video_bytes") or 0) and _sha256(video) == item["video_sha256"]
        if valid and item.get("srt_sha256"):
            valid = subtitle.stat().st_size == int(item.get("srt_bytes") or 0) and _sha256(subtitle) == item["srt_sha256"]
        if valid and not item.get("video_sha256"):
            try:
                validate_video(str(video))
            except ArtifactValidationError:
                valid = False
        if valid and not item.get("srt_sha256"):
            try:
                validate_srt(str(subtitle))
            except ArtifactValidationError:
                valid = False
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
              "final_merge_ready": False,
              "all_worker_ids": [int(item["worker_id"]) for item in worker_matrix]}

    for run_id in list_candidate_runs(repo, current_run_id, explicit_source):
        artifacts = list_run_artifacts(repo, run_id)
        if "shared-config" not in artifacts:
            continue
        with tempfile.TemporaryDirectory() as temporary:
            shared = download_artifact(repo, artifacts["shared-config"], Path(temporary) / "shared")
            source_config_path = _find(shared, "config.yaml")
            if not source_config_path:
                if explicit_source:
                    raise ArtifactValidationError("explicit source Run shared-config is ambiguous or incomplete")
                continue
            source_config = yaml.safe_load(source_config_path.read_text(encoding="utf-8")) or {}
            if config_fingerprint(source_config) != config_fingerprint(config):
                if explicit_source:
                    raise ArtifactValidationError("explicit source Run config fingerprint mismatch")
                continue
        result["source_run_id"] = run_id
        result["artifacts"] = artifacts
        break
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
            result["all_part_numbers"] = [int(part["part_num"]) for part in plan["parts"]]
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
                _, ledger = validate_publication(output / "publication", plan, config, require_complete=False)
            except Exception as error:
                result.setdefault("rejected", []).append(f"publication: {error}")
                shutil.rmtree(output / "publication", ignore_errors=True)
            else:
                result.setdefault("notes", []).append(f"publication checkpoint is resumable: {complete_error}")
                for position, part in enumerate(sorted(plan["parts"], key=lambda x: int(x["part_num"]))):
                    steps = ((ledger.get("parts") or {}).get(str(int(part["part_num"])), {}).get("steps") or {})
                    if any((steps.get(step) or {}).get("status") != "completed" for step in PART_STEPS):
                        result["publication_resume_position"] = position
                        break

    if plan and "final-merge" in artifacts:
        try:
            root = download_artifact(repo, artifacts["final-merge"], output / "final-merge")
            validate_final_merge(root, plan)
            result.update(mode="publication", worker_matrix=empty, merge_matrix=empty, final_merge_ready=True)
            _write_plan(result, output); return result
        except TransientRemoteValidationError:
            raise
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
            except TransientRemoteValidationError:
                raise
            except Exception as error:
                result.setdefault("rejected", []).append(f"{name}: {error}")
                shutil.rmtree(merge_root / name, ignore_errors=True)
        all_parts = {int(p["part_num"]) for p in plan["parts"]}
        missing_parts = sorted(all_parts - set(recovered_parts))
        if not missing_parts:
            result.update(mode="final_merge", worker_matrix=empty, merge_matrix=empty)
            _write_plan(result, output); return result
        original = (plan.get("matrix") or {}).get("include") or []
        preserved = []
        missing = set(missing_parts)
        for assignment in original:
            assigned = [int(x) for x in str(assignment.get("part_numbers", "")).split(",") if x.strip()]
            retained = [number for number in assigned if number in missing]
            if retained:
                preserved.append({"merge_worker_id": assignment.get("merge_worker_id", len(preserved)),
                                  "part_numbers": ",".join(map(str, retained))})
        covered = [int(x) for item in preserved for x in item["part_numbers"].split(",")]
        result["merge_matrix"] = ({"include": preserved} if len(preserved) <= 17 and set(covered) == missing
                                  else build_merge_matrix(missing_parts))

    # 3. Only now inspect Workers. Completed workers are omitted; partial ones
    # carry their validated workspace in the resume bundle.
    needed_chapters = set()
    if plan and result["merge_matrix"]["include"]:
        missing_part_numbers = {int(number) for item in result["merge_matrix"]["include"]
                                for number in item["part_numbers"].split(",")}
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
    rerun_workers = [int(item["worker_id"]) for item in (result.get("worker_matrix") or {}).get("include", [])]
    serializable["rerun_worker_ids"] = rerun_workers
    serializable["reused_worker_ids"] = [number for number in result.get("all_worker_ids", [])
                                         if number not in set(rerun_workers)]
    rerun_parts = [int(number) for item in (result.get("merge_matrix") or {}).get("include", [])
                   for number in str(item.get("part_numbers", "")).split(",") if number]
    serializable["rerun_part_numbers"] = rerun_parts
    serializable["reused_part_numbers"] = [number for number in result.get("all_part_numbers", [])
                                           if number not in set(rerun_parts)]
    serializable.setdefault("publication_resume_position", None)
    result.update({key: serializable[key] for key in (
        "rerun_worker_ids", "reused_worker_ids", "rerun_part_numbers",
        "reused_part_numbers", "publication_resume_position")})
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
