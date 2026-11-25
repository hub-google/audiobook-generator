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
from dataclasses import dataclass

import requests


MAX_NATIVE_SCRAPE_RERUNS = 20
POLL_SECONDS = 5
SCRAPE_RUN_PREFIX = "【TXT抓取】"
SCRAPE_JOB_PREFIX = "🕷️ Scrape — Worker"


@dataclass(frozen=True)
class RerunDecision:
    should_rerun: bool
    failed_job_ids: tuple[int, ...] = ()
    reason: str = ""
    exhausted: bool = False


def _attempt(run: dict) -> int:
    try:
        return max(1, int(run.get("run_attempt") or 1))
    except (TypeError, ValueError):
        return 1


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


class GitHubActions:
    def __init__(self, repository: str, token: str):
        self.base_url = f"https://api.github.com/repos/{repository}"
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_run(self, run_id: str) -> dict:
        response = requests.get(
            f"{self.base_url}/actions/runs/{run_id}",
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def get_jobs(self, run_id: str) -> list[dict]:
        response = requests.get(
            f"{self.base_url}/actions/runs/{run_id}/jobs",
            headers=self.headers,
            params={"per_page": 100},
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("jobs", [])

    def rerun_failed_jobs(self, run_id: str) -> None:
        response = requests.post(
            f"{self.base_url}/actions/runs/{run_id}/rerun-failed-jobs",
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()


def _wait_for_completion(client: GitHubActions, run_id: str) -> dict:
    while True:
        run = client.get_run(run_id)
        if run.get("status") == "completed":
            return run
        print(
            f"Source Run {run_id} is {run.get('status')}; "
            f"waiting {POLL_SECONDS}s before checking again.",
            flush=True,
        )
        time.sleep(POLL_SECONDS)


def run_controller(client: GitHubActions, run_id: str) -> str:
    """Run all immediate native reruns and return the final outcome label."""
    rerun_count = 0
    while True:
        run = _wait_for_completion(client, run_id)
        jobs = client.get_jobs(run_id)
        decision = decide_rerun(run, jobs)

        if not decision.should_rerun:
            if decision.exhausted:
                print(
                    f"Source Run {run_id} exhausted the {MAX_NATIVE_SCRAPE_RERUNS}-rerun limit; "
                    "leaving it failed for needs_attention.",
                    flush=True,
                )
                return "needs_attention"
            print(f"No scraper rerun needed for Run {run_id}: {decision.reason}.", flush=True)
            return decision.reason

        client.rerun_failed_jobs(run_id)
        rerun_count += 1
        print(
            f"Immediately requested native scraper rerun {rerun_count}/"
            f"{MAX_NATIVE_SCRAPE_RERUNS} for Run {run_id}; "
            f"failed jobs: {', '.join(map(str, decision.failed_job_ids))}.",
            flush=True,
        )
        # The next loop waits for this same Run's new attempt to complete.


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
