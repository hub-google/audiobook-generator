"""Immediately rerun failed TXT scraper jobs on the same Actions Run.

The controller is dispatched by the parent scrape workflow after its matrix
has finished.  It waits until the parent Run is completed, then uses GitHub's
native ``rerun-failed-jobs`` endpoint.  Each native rerun gets a fresh
GitHub-hosted runner, which is required for source sites that block an
egress IP.  The controller owns all 20 reruns so no scheduler delay is used.
"""

from __future__ import annotations

import os
import sys
import time
try:
    from .native_rerun import RerunDecision, GitHubActions, attempt_number as _attempt, run_native_controller
except ImportError:
    from native_rerun import RerunDecision, GitHubActions, attempt_number as _attempt, run_native_controller


MAX_NATIVE_SCRAPE_RERUNS = 20
POLL_SECONDS = 5
SCRAPE_RUN_PREFIX = "【TXT抓取】"
SCRAPE_JOB_PREFIX = "🕷️ Scrape — Worker"


def failed_scrape_job_ids(jobs: list[dict]) -> tuple[int, ...]:
    return tuple(
        int(job["id"])
        for job in jobs
        if job.get("conclusion") == "failure"
        and str(job.get("name") or "").startswith(SCRAPE_JOB_PREFIX)
        and str(job.get("id") or "").isdigit()
    )


def decide_rerun(run: dict, jobs: list[dict]) -> RerunDecision:
    """Decide whether this completed Run needs a native scraper rerun."""
    if run.get("status") != "completed":
        return RerunDecision(False, reason="source_run_not_completed")
    if run.get("conclusion") != "failure":
        return RerunDecision(False, reason="source_run_not_failed")

    run_name = str(run.get("name") or run.get("display_title") or "")
    if not run_name.startswith(SCRAPE_RUN_PREFIX):
        return RerunDecision(False, reason="not_a_txt_scrape_run")

    failed_ids = failed_scrape_job_ids(jobs)
    if not failed_ids:
        return RerunDecision(False, reason="no_failed_scrape_jobs")

    if _attempt(run) > MAX_NATIVE_SCRAPE_RERUNS:
        return RerunDecision(
            False,
            failed_job_ids=failed_ids,
            reason="scrape_rerun_limit_reached",
            exhausted=True,
        )

    return RerunDecision(True, failed_job_ids=failed_ids, reason="failed_scrape_jobs")


def run_controller(client: GitHubActions, run_id: str) -> str:
    return run_native_controller(client, run_id, decide_rerun, MAX_NATIVE_SCRAPE_RERUNS, "SCRAPE", POLL_SECONDS)


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
    print(f"Scraper rerun controller finished: {outcome}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
