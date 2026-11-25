"""GitHub REST/CLI gateway for queue dispatch and run reconciliation.

No durable task state is written here. The gateway owns only HTTP access and
response decoding, so dispatch policy and reconciliation can be tested without
network calls.
"""
import requests
import subprocess


class GitHubRunGateway:
    def request(self, method, path, **kwargs):
        response = requests.request(method, self.api + path, headers=self.headers, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API {method} {path} failed ({response.status_code}): {response.text}")
        return response

    def runs(self):
        return self.request("GET", "/actions/workflows/audiobook.yml/runs", params={"event": "workflow_dispatch", "per_page": 100}).json().get("workflow_runs", [])

    def cover_runs(self):
        return self.request("GET", "/actions/workflows/cover-preflight.yml/runs", params={"event": "workflow_dispatch", "per_page": 100}).json().get("workflow_runs", [])

    def run_by_id(self, run_id):
        response = requests.get(f"{self.api}/actions/runs/{run_id}", headers=self.headers, timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API GET /actions/runs/{run_id} failed ({response.status_code}): {response.text}")
        return response.json()

    def run_artifact_names(self, run_id):
        """Return non-expired artifact names for one existing Actions Run."""
        response = self.request(
            "GET", f"/actions/runs/{run_id}/artifacts", params={"per_page": 100},
        )
        return {
            str(item.get("name") or "")
            for item in response.json().get("artifacts", [])
            if not item.get("expired") and item.get("name")
        }

    def run_uses_current_master(self, run_id):
        """Only rerun in place when GitHub would execute the current code."""
        run = self.run_by_id(run_id)
        if not run or not run.get("head_sha"):
            return False
        master = self.request("GET", "/git/ref/heads/master").json()
        master_sha = ((master.get("object") or {}).get("sha") or "").strip()
        return bool(master_sha and run["head_sha"] == master_sha)

    def run_jobs(self, run_id):
        try:
            response = requests.get(
                f"{self.api}/actions/runs/{run_id}/jobs",
                headers=self.headers, params={"per_page": 100}, timeout=30,
            )
            if response.status_code == 200:
                return response.json().get("jobs", [])
        except Exception:
            pass
        return []

    def job_log(self, job_id):
        # Late binding keeps CLI injection and test patches on the public
        # Dispatcher module effective without a circular import on startup.
        from .queue_dispatcher import _find_gh
        try:
            response = requests.get(
                f"{self.api}/actions/jobs/{job_id}/logs",
                headers=self.headers, timeout=15, allow_redirects=True,
            )
            if response.status_code == 200 and response.text:
                return response.text
        except Exception:
            pass
        try:
            res = subprocess.run(
                [_find_gh(), "run", "view", f"--job={job_id}", "--repo", self.repo, "--log"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, check=False,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        return ""

