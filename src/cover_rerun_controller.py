"""Immediately rerun failed cover-preflight jobs on the same Actions Run.

The cover workflow dispatches this controller after its jobs have settled.  The
controller waits for the source Run to complete and uses GitHub's native
``rerun-failed-jobs`` endpoint, preserving successful upstream artifacts while
retrying only failed work and its downstream dependants.
"""

from __future__ import annotations

import os
import sys
import time
try:
    from .native_rerun import RerunDecision, GitHubActions, attempt_number as _attempt, run_native_controller
except ImportError:
    from native_rerun import RerunDecision, GitHubActions, attempt_number as _attempt, run_native_controller


MAX_NATIVE_COVER_RERUNS = 20
POLL_SECONDS = 5
COVER_RUN_PREFIX = "【封面與Gemini】"
COVER_JOB_PREFIXES = (
    "📚 Generate Gemini book introduction",
    "🧠 Generate Gemini cover information",
    "🔎 Review Gemini cover information",
    "🎨 Generate HF cover",
    # Keep compatibility with cover Runs created before the four-stage split.
    "🎨 Generate cover and Gemini review data",
)


def failed_cover_job_ids(jobs: list[dict]) -> tuple[int, ...]:
    return tuple(
        int(job["id"])
        for job in jobs
        if job.get("conclusion") == "failure"
        and str(job.get("name") or "").startswith(COVER_JOB_PREFIXES)
        and str(job.get("id") or "").isdigit()
    )


def decide_rerun(run: dict, jobs: list[dict]) -> RerunDecision:
    if run.get("status") != "completed":
        return RerunDecision(False, reason="source_run_not_completed")
    if run.get("conclusion") != "failure":
        return RerunDecision(False, reason="source_run_not_failed")

    run_name = str(run.get("display_title") or run.get("name") or "")
    if not run_name.startswith(COVER_RUN_PREFIX):
        return RerunDecision(False, reason="not_a_cover_preflight_run")

    failed_ids = failed_cover_job_ids(jobs)
    if not failed_ids:
        return RerunDecision(False, reason="no_failed_cover_jobs")

    if _attempt(run) > MAX_NATIVE_COVER_RERUNS:
        return RerunDecision(
            False,
            failed_job_ids=failed_ids,
            reason="cover_rerun_limit_reached",
            exhausted=True,
        )

    return RerunDecision(True, failed_job_ids=failed_ids, reason="failed_cover_jobs")


def run_controller(client: GitHubActions, run_id: str) -> str:
    return run_native_controller(client, run_id, decide_rerun, MAX_NATIVE_COVER_RERUNS, "COVER", POLL_SECONDS)


def main() -> int:
    run_id = os.environ.get("SOURCE_RUN_ID", "").strip()
    repository = os.environ.get("REPOSITORY", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    if not run_id or not run_id.isdigit():
        print("SOURCE_RUN_ID must be a numeric GitHub Actions Run ID.", file=sys.stderr)
        return 2
    if not repository or not token:
        print("REPOSITORY and GH_TOKEN are required.", file=sys.stderr)
        return 2

    outcome = run_controller(GitHubActions(repository, token), run_id)
    print(f"Cover rerun controller finished: {outcome}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
