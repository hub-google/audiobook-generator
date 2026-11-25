"""Production GitHub Actions checks moved out of YAML for direct unit testing.

Each entrypoint preserves the former inline Python code and all failure gates.
Run with: python -m src.ci.audiobook_steps <step-name>
"""
import argparse


def validate_resume_title():
    import os
    import yaml

    with open("config.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    expected_title = str(config.get("book_title") or "").strip()
    supplied_title = os.environ.get("DISPATCH_BOOK_TITLE", "").strip()
    chapter_label = os.environ.get("DISPATCH_CHAPTER_LABEL", "").strip()
    queue_task_id = os.environ.get("DISPATCH_QUEUE_TASK_ID", "").strip()
    missing = [name for name, value in (
        ("book_title", supplied_title),
        ("chapter_label", chapter_label),
        ("queue_task_id", queue_task_id),
    ) if not value]
    if missing:
        raise SystemExit(
            "Strict resume dispatch validation failed: missing original input(s): "
            + ", ".join(missing)
        )
    if supplied_title != expected_title:
        raise SystemExit(
            "Strict resume dispatch validation failed: book_title "
            f"{supplied_title!r} does not match source config {expected_title!r}"
        )


def validate_chapter_matrix():
    import json
    import yaml

    with open("config.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    with open("matrix.json", encoding="utf-8") as handle:
        matrix = (json.load(handle) or {}).get("include") or []
    selected = [int(value) for value in config.get("selected_indices") or []]
    patterns = (config.get("cleaner") or {}).get("remove_patterns") or []
    if not isinstance(patterns, list):
        raise SystemExit("Strict success gate: cleaner remove_patterns must be a list")
    if patterns and not (config.get("cleaner") or {}).get("fingerprint"):
        raise SystemExit("Strict success gate: cleaner profile fingerprint is missing")
    if not selected:
        raise SystemExit("Strict success gate: no chapters were selected")
    if not matrix:
        raise SystemExit("Strict success gate: no workers were generated")
    if len(matrix) > 17:
        raise SystemExit(
            f"Strict success gate: matrix generated {len(matrix)} workers; maximum is 17"
        )
    worker_total = sum(int(item["end_chap"]) - int(item["start_chap"]) + 1 for item in matrix)
    if worker_total < len(selected):
        raise SystemExit(
            f"Strict success gate: matrix covers {worker_total} slots for {len(selected)} selected chapters"
        )


def write_setup_summary():
    import os, yaml, json
    with open("config.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    with open("matrix.json", encoding="utf-8") as handle:
        matrix = (json.load(handle) or {}).get("include") or []
    cleaner = config.get("cleaner") or {}
    selected = [int(value) for value in config.get("selected_indices") or []]
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write("## 📚 Audiobook Pipeline Started\n")
        summary.write(f"✅ **Book:** {config.get('book_title')}\n")
        summary.write(f"✅ **Target URL:** {config.get('catalog_url')}\n")
        summary.write(f"✅ **Chapter Range:** {min(selected)} ~ {max(selected)}\n")
        summary.write(f"✅ **Actual Selected Chapters:** {len(selected)}\n")
        summary.write(f"✅ **Workers Created:** {len(matrix)}\n")
        summary.write(f"✅ **Resume Source Run:** {os.environ.get('SOURCE_RUN_ID') or 'none'}\n")
        summary.write(f"✅ **Book Profile:** `{config.get('book_profile_id') or 'manual/default'}` revision {config.get('profile_revision', 0)}\n")
        summary.write(f"✅ **Cleaner Rules:** {len(cleaner.get('remove_patterns') or [])}\n")
        summary.write(f"✅ **Cleaner Fingerprint:** `{(cleaner.get('fingerprint') or 'none')[:12]}`\n")


def write_resume_summary():
    import json, os
    plan = json.load(open("resume_bundle/resume-plan.json", encoding="utf-8"))
    lines = ["## Global Resume Planner", "",
             f"- Selected source Run: {plan.get('source_run_id') or 'none'}",
             f"- Deepest resume mode: {plan.get('mode')}",
             f"- Reused worker IDs: {plan.get('reused_worker_ids', [])}",
             f"- Rerun worker IDs: {plan.get('rerun_worker_ids', [])}",
             f"- Reused Part numbers: {plan.get('reused_part_numbers', [])}",
             f"- Rerun Part numbers: {plan.get('rerun_part_numbers', [])}",
             f"- Publication resume position: {plan.get('publication_resume_position')}"]
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(" | ".join(lines[2:]))


def write_worker_summary():
    import os
    import yaml
    from src.pipeline_checkpoint import PipelineCheckpoint
    from src.worker_pipeline import build_stage_settings, extract_chapter_sources

    with open("config.yaml", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    book_title = config["book_title"]
    from src.source_identity import workspace_name
    workspace = os.path.abspath(os.path.join(config["paths"]["workspace_base"], workspace_name(config)))
    worker_id = int(os.environ["WORKER_ID"])
    per_worker = int(config["chapters_per_worker"])
    start = worker_id * per_worker
    chapters = config["selected_indices"][start:start + per_worker]
    sources = extract_chapter_sources(config)
    checkpoint = PipelineCheckpoint(
        workspace, book_title, worker_id, chapters,
        cleaner_fingerprint=(config.get("cleaner") or {}).get("fingerprint", ""),
        stage_settings=build_stage_settings(config),
        validation_cache_enabled=(config.get("validation") or {}).get("cache_enabled", True),
        chapter_sources=sources,
        source_chapter_sources=sources,
    )
    incomplete = checkpoint.incomplete_chapters()
    missing = checkpoint.source_missing_chapters()
    print("[RUN_SUMMARY] " + __import__("json").dumps({
        "kind": "worker", "worker": worker_id,
        "completed": len(chapters) - len(incomplete) - len(missing),
        "total": len(chapters),
        "incomplete": incomplete,
        "source_missing": missing,
    }, ensure_ascii=False))
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(checkpoint.markdown_summary())


def check_final_merge():
    import json
    from src.resume_planner import validate_final_merge
    plan = json.load(open("prepared_plan/parts-plan.json", encoding="utf-8"))
    validate_final_merge("final_merge", plan)


def validate_youtube_checkpoint():
    import json, os, sys, yaml
    from src.publication_checkpoint import PublicationCheckpoint, plan_structure_fingerprint
    from src.youtube_upload.state import load_resume_state

    state_file = "upload_resume_state/state.json"
    state = load_resume_state(state_file)
    if not state:
        sys.exit(0)

    config_path = "prepared_source/config.yaml"
    book_profile_id = ""
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            book_profile_id = str(cfg.get("book_profile_id") or "").strip()

    if book_profile_id and state.get("book_profile_id") and str(state["book_profile_id"]).strip() != book_profile_id:
        print(f"::error title=Checkpoint rejected::Checkpoint book_profile_id {state['book_profile_id']!r} != expected {book_profile_id!r}")
        sys.exit(1)

    plan_path = "prepared_plan/parts-plan.json"
    if os.path.exists(plan_path) and state.get("part_plan"):
        with open(plan_path, encoding="utf-8") as f:
            locked = json.load(f)
        expected_fp = plan_structure_fingerprint(locked.get("parts", []))
        actual_fp = plan_structure_fingerprint(state["part_plan"])
        if expected_fp != actual_fp:
            print("::error title=Checkpoint rejected::part_plan fingerprint differs from current prepared plan")
            sys.exit(1)

    publication = PublicationCheckpoint(state_file)
    if publication.is_locked():
        valid, reason = publication.validate_task_identity(book_profile_id=book_profile_id)
        if not valid:
            print(f"::error title=Checkpoint rejected::{reason}")
            sys.exit(1)

    print("Restored YouTube upload checkpoint validated successfully.")


def enforce_publication_evidence():
    import os
    from src.success_criteria import validate_upload_success

    result = validate_upload_success(
        "upload_resume_state/state.json",
        expected_run_id=os.environ.get("SOURCE_RUN_ID"),
        execution_run_id=os.environ.get("EXECUTION_RUN_ID"),
        expected_book_profile_id=os.environ.get("BOOK_PROFILE_ID"),
    )
    print(
        "Strict success gate passed: "
        f"{result['parts']} Parts published to {result['playlist_url']}"
    )


def write_publication_summary():
    import json, os
    from src.publication_checkpoint import PublicationCheckpoint

    state_file = "upload_resume_state/state.json"
    if not os.path.exists(state_file):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## YouTube Upload Status\nNo state file found.\n")
        exit(0)

    with open(state_file, encoding="utf-8") as f:
        state = json.load(f)
    hf_state_file = "upload_resume_state/hf_archive_state.json"
    hf_state = {}
    if os.path.exists(hf_state_file):
        with open(hf_state_file, encoding="utf-8") as f:
            hf_state = json.load(f)

    done = len(state.get("completed_titles", []))
    total = len(state.get("part_plan", [])) or max(done, 1)
    pending = len(state.get("pending_thumbnails", {}))
    pending_playlist = len(state.get("pending_playlist", {}))
    pending_captions = len(state.get("pending_captions", {}))
    pending_publish = len(state.get("pending_publish", {}))
    viewer_gate = state.get("final_playlist_validation") or {}
    status = state.get("status", "unknown")
    publication = PublicationCheckpoint(state_file)
    book_title = publication.data.get("book_title", "")
    books = hf_state.get("books", {})
    hf_repo = hf_state.get("repo_id", "尚未建立")
    hf_book_key = publication.data.get('source_fingerprint') or book_title
    current_hf_book = books.get(hf_book_key, {}) if hf_book_key else {}
    hf_parts = (current_hf_book.get("parts") or {}).values()
    hf_done = sum(item.get("status") == "complete" for item in hf_parts)
    print("[RUN_SUMMARY] " + json.dumps({
        "kind": "youtube", "status": status, "completed": done, "total": total,
        "pending_thumbnails": pending, "pending_playlist": pending_playlist,
        "pending_captions": pending_captions, "pending_publish": pending_publish,
        "reason": state.get("reason"), "retry_at": state.get("retry_at"),
        "playlist_url": state.get("playlist_url"),
        "viewer_playlist_gate": viewer_gate.get("status", "missing"),
        "viewer_playlist_items": viewer_gate.get("item_count", 0),
        "viewer_playlist_order": viewer_gate.get("ordered_parts", []),
    }, ensure_ascii=False))

    status_icon = "⏸️"
    if status == "complete": status_icon = "🎉"
    elif status == "failed": status_icon = "❌"
    elif status == "unknown": status_icon = "⚠️"

    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
        f.write(f"## {status_icon} YouTube Upload Status: {status.upper()}\n\n")
        f.write(f"| 項目 | 狀態資訊 |\n|---|---|\n")
        f.write(f"| **任務進度** | **{done} / {total} Parts** ({done/total*100:.1f}%) |\n")
        f.write(f"| **Pending Thumbnails** | **{pending}** |\n")
        f.write(f"| **Pending Playlist Inserts** | **{pending_playlist}** |\n")
        f.write(f"| **Pending CC Captions** | **{pending_captions}** |\n")
        f.write(f"| **Pending Final Publish** | **{pending_publish}** |\n")
        f.write(f"| **使用者最終播放清單驗收** | **{viewer_gate.get('status', 'missing').upper()}** |\n")
        f.write(f"| **播放清單實際項目數** | **{viewer_gate.get('item_count', 0)}** |\n")
        f.write(f"| **Part 排序** | **{viewer_gate.get('ordered_parts', [])}** |\n")
        f.write(f"| **Current Book Hugging Face Parts** | **{hf_done} / {total}** |\n")
        if not book_title:
            f.write("| **Hugging Face Summary Warning** | `本次 checkpoint 缺少 book_title，無法統計本書 HF Parts` |\n")
        f.write(f"| **Hugging Face Repo** | `{hf_repo}` |\n")
        if "reason" in state and state["reason"]:
            f.write(f"| **Reason** | `{state['reason']}` |\n")
        if "retry_at" in state and state["retry_at"]:
            f.write(f"| **Retry After** | `{state['retry_at']}` |\n")

        playlist_url = state.get("playlist_url")
        if playlist_url and playlist_url.startswith("http"):
            f.write(f"| **播放清單** | [點此開啟 YouTube 播放清單]({playlist_url}) |\n")
        f.write("\n")
        f.write(publication.markdown_summary())


STEP_NAMES = ['validate_resume_title','validate_chapter_matrix','write_setup_summary','write_resume_summary','write_worker_summary','check_final_merge','validate_youtube_checkpoint','enforce_publication_evidence','write_publication_summary']


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audiobook GitHub workflow step")
    parser.add_argument("step", choices=STEP_NAMES)
    args = parser.parse_args(argv)
    return globals()[args.step]()


if __name__ == "__main__":
    main()
