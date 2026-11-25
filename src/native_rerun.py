"""Shared native Actions retry controller for independent preflight workflows."""
from __future__ import annotations
from dataclasses import dataclass
import time
import requests

@dataclass(frozen=True)
class RerunDecision:
    should_rerun: bool
    failed_job_ids: tuple[int, ...] = ()
    reason: str = ""
    exhausted: bool = False

def attempt_number(run):
    try:
        return max(1, int(run.get("run_attempt") or 1))
    except (TypeError, ValueError):
        return 1

class GitHubActions:
    def __init__(self, repository, token):
        self.base_url = f"https://api.github.com/repos/{repository}"
        self.headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}

    def get_run(self, run_id):
        response = requests.get(f"{self.base_url}/actions/runs/{run_id}", headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_jobs(self, run_id):
        jobs = []
        page = 1
        while True:
            response = requests.get(f"{self.base_url}/actions/runs/{run_id}/jobs", headers=self.headers, params={"per_page": 100, "page": page}, timeout=30)
            response.raise_for_status()
            batch = response.json().get("jobs", [])
            jobs.extend(batch)
            if len(batch) < 100:
                return jobs
            page += 1

    def rerun_failed_jobs(self, run_id):
        response = requests.post(f"{self.base_url}/actions/runs/{run_id}/rerun-failed-jobs", headers=self.headers, timeout=30)
        response.raise_for_status()

def wait_for_completion(client, run_id, after_attempt=0, poll_seconds=5):
    deadline = time.monotonic() + 5 * 60 * 60
    while time.monotonic() < deadline:
        run = client.get_run(run_id)
        if run.get("status") == "completed" and attempt_number(run) > after_attempt:
            return run
        time.sleep(poll_seconds)
    raise TimeoutError(f"Source Run {run_id} did not complete a new attempt")

def run_native_controller(client, run_id, decide, limit, label, poll_seconds=5):
    previous_attempt = 0
    while True:
        run = wait_for_completion(client, run_id, previous_attempt, poll_seconds)
        decision = decide(run, client.get_jobs(run_id))
        if not decision.should_rerun:
            print(f"{label} {run_id}: {decision.reason}", flush=True)
            return "needs_attention" if decision.exhausted else decision.reason
        client.rerun_failed_jobs(run_id)
        previous_attempt = attempt_number(run)
        print(f"{label} {run_id}: native rerun {previous_attempt}/{limit}, jobs={decision.failed_job_ids}", flush=True)
