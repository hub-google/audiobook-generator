"""GitHub observation and cloud queue reconciliation."""
from .runtime import (
    json, logging, time, requests, threading, re, datetime, ThreadPoolExecutor, as_completed, BLOCKING_STATES, preflight_status, GitHubQueueStore, task_id_from_run_name, error_observation, missing_observation, successful_observation,
)
from copy import deepcopy

class QueueMonitorMixin:
    @staticmethod
    def _observation_checked_time(observation):
        if not observation or not observation.get("checked_at"):
            return "—"
        try:
            value = datetime.fromisoformat(str(observation["checked_at"]).replace("Z", "+00:00"))
            return value.astimezone().strftime("%H:%M:%S")
        except (TypeError, ValueError):
            return "無效時間"

    @staticmethod
    def _http_error_observation(response):
        status = response.status_code
        if status == 401:
            code = "unauthorized"
        elif status == 429 or response.headers.get("X-RateLimit-Remaining") == "0":
            code = "rate_limited"
        elif status == 403:
            code = "forbidden"
        elif status >= 500:
            code = "github_error"
        else:
            code = "invalid_response"
        return error_observation(code, http_status=status, detail=response.text[:500])

    def _observe_github_run(self, repo, token, run_id, previous):
        headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        for attempt in range(2):
            try:
                response = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/runs/{run_id}",
                    headers=headers, timeout=15,
                )
                if response.status_code == 200:
                    try:
                        return successful_observation(response.json())
                    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
                        return error_observation("invalid_response", http_status=200, detail=error)
                if response.status_code == 404:
                    verification = requests.get(
                        f"https://api.github.com/repos/{repo}/actions/runs?per_page=1",
                        headers=headers, timeout=15,
                    )
                    if verification.status_code != 200:
                        return self._http_error_observation(verification)
                    return missing_observation(previous, confirmed=True)
                return self._http_error_observation(response)
            except requests.RequestException as error:
                if attempt == 1:
                    return error_observation("network_error", detail=error)
                time.sleep(1)

    def _refresh_observation_freshness(self):
        """Expire an old observation in the UI even while a network poll is blocked."""
        if self.cloud_queue:
            for task in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", []):
                task_id = task.get("task_id")
                display_tree = next((tree for tree in (self.pending_queue_tree, self.success_queue_tree) if tree.exists(task_id)), None)
                if task_id and task.get("run_id") and display_tree is not None:
                    observation = self.github_observations.get(task_id)
                    display_tree.set(task_id, "status", self._queue_status_text(task))
                    display_tree.set(task_id, "verified", self._observation_checked_time(observation))
        self.queue_freshness_after = self.root.after(1000, self._refresh_observation_freshness)

    def _schedule_queue_refresh(self):
        self.queue_syncing = False
        self.queue_sync_after = self.root.after(10000, self.sync_cloud_queue)

    def sync_cloud_queue(self):
        if self.queue_sync_after is not None:
            try:
                self.root.after_cancel(self.queue_sync_after)
            except Exception:
                pass
            self.queue_sync_after = None
        if self.queue_syncing:
            return
        self.queue_syncing = True
        def worker():
            try:
                store, repo, token = self._queue_store()
                queue, _ = store.load()
                terminal_updates = []
                running_updates = []
                preflight_updates = {}
                # Preflight owns two independent Runs.  The queue dispatcher
                # reconciles both atomically; the legacy single-Run observer
                # must not mark the book complete when only TXT has finished.
                monitored = [
                    task for task in queue.get("queue", [])
                    if task.get("run_id") and task.get("workflow_phase") != "preflight"
                ]
                observations = {}
                preflight_stages = []
                for task in queue.get("queue", []):
                    if task.get("workflow_phase") != "preflight":
                        continue
                    for stage_name, prefix, workflow_file in (
                        ("scrape", f"scrape-review-{task.get('task_id')}", "audiobook.yml"),
                        ("cover", f"cover-review-{task.get('task_id')}", "cover-preflight.yml"),
                    ):
                        run_id = task.get(f"{stage_name}_run_id") or \
                            ((task.get("stages") or {}).get(stage_name) or {}).get("run_id")
                        stage = ((task.get("stages") or {}).get(stage_name) or {})
                        # A manual retry may create a newer Run without updating
                        # the durable queue record.  Once the bound Run failed,
                        # discover and adopt the newest matching retry.
                        if not run_id or stage.get("status") == "failed":
                            discovered = self._discover_preflight_run(
                                repo, token, workflow_file, task["task_id"],
                                stage.get("dispatched_at") or task.get("dispatched_at"),
                            )
                            discovered_id = int(discovered.get("id")) if discovered else None
                            if discovered_id and (not run_id or discovered_id > int(run_id)):
                                run_id = discovered_id
                        if run_id:
                            preflight_stages.append((task["task_id"], stage_name, int(run_id), prefix))
                worker_count = min(8, max(1, len(monitored) + len(preflight_stages)))
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = {
                        executor.submit(
                            self._observe_github_run, repo, token, task["run_id"],
                            self.github_observations.get(task["task_id"]),
                        ): task
                        for task in monitored
                    }
                    preflight_futures = {
                        executor.submit(
                            self._observe_preflight_stage, repo, token, run_id, prefix,
                        ): (task_id, stage_name, run_id)
                        for task_id, stage_name, run_id, prefix in preflight_stages
                    }
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            observations[task["task_id"]] = future.result()
                        except Exception as error:
                            observations[task["task_id"]] = error_observation("network_error", detail=error)
                    for future in as_completed(preflight_futures):
                        task_id, stage_name, run_id = preflight_futures[future]
                        try:
                            result = future.result()
                        except Exception as error:
                            logging.warning("Could not reconcile %s %s Run %s: %s", task_id, stage_name, run_id, error)
                            continue
                        preflight_updates.setdefault(task_id, {})[stage_name] = {
                            "run_id": run_id, **result,
                        }
                for task in monitored:
                    run_id = task["run_id"]
                    observation = observations[task["task_id"]]
                    self.github_observations[task["task_id"]] = observation
                    if observation.get("kind") == "ok" and observation.get("raw_status") != "completed":
                        if task.get("status") != "running":
                            running_updates.append((task["task_id"], run_id))
                    elif task.get("status") in BLOCKING_STATES:
                        if observation.get("kind") == "not_found" and observation.get("confirmed_missing"):
                            terminal_updates.append((task["task_id"], run_id, "interrupted", "run_not_found", "missing", None))
                        elif (observation.get("kind") == "ok" and
                              observation.get("raw_status") == "completed" and
                              observation.get("raw_conclusion") == "cancelled"):
                            terminal_updates.append((
                                task["task_id"], run_id, "interrupted", "run_cancelled", "cancelled",
                                observation.get("github_updated_at"),
                            ))
                        elif (observation.get("kind") == "ok" and
                              observation.get("raw_status") == "completed" and
                              observation.get("raw_conclusion") == "success"):
                            terminal_updates.append((
                                task["task_id"], run_id, "completed", None, "success",
                                observation.get("github_updated_at"),
                            ))
                        elif (observation.get("kind") == "ok" and
                              observation.get("raw_status") == "completed" and
                              observation.get("raw_conclusion") != "success"):
                            conclusion = observation.get("raw_conclusion") or "unknown"
                            terminal_updates.append((
                                task["task_id"], run_id, "waiting_retry",
                                f"run_{conclusion}", conclusion,
                                observation.get("github_updated_at"),
                            ))
                # Read-only GUI observer: the dispatcher is the sole writer of
                # durable run/preflight state. Never replay stale GET results
                # through GitHubQueueStore.mutate from this local window.
                if terminal_updates:
                    self._dispatch_queue_workflow()
                display_queue = deepcopy(queue)
                if preflight_updates:
                    self._apply_preflight_observations(display_queue, preflight_updates)
                self.cloud_queue = queue
                self.root.after(0, lambda snapshot=display_queue: self._render_queue(snapshot))
            except Exception as error:
                if self.cloud_queue:
                    for task in self.cloud_queue.get("queue", []) + self.cloud_queue.get("completed", []):
                        if task.get("run_id"):
                            self.github_observations[task["task_id"]] = error_observation(
                                "network_error", detail=error,
                            )
                    self.root.after(0, lambda q=self.cloud_queue: self._render_queue(q))
                self.root.after(0, lambda e=str(error): self.log(f"⚠ 雲端佇列同步失敗：{e}"))
            finally:
                # Tk's event scheduler must be accessed on the GUI thread.
                self.root.after(0, self._schedule_queue_refresh)
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _apply_preflight_observations(queue, observations):
        """Apply verified preflight Run states without overwriting a newer Run."""
        changed = False
        for task in queue.get("queue", []):
            task_id = task.get("task_id")
            if task.get("workflow_phase") != "preflight" or task_id not in observations:
                continue
            stages = task.setdefault("stages", {})
            for stage_name, result in observations[task_id].items():
                stage = stages.setdefault(stage_name, {})
                run_id = int(result["run_id"])
                bound_run_id = task.get(f"{stage_name}_run_id") or stage.get("run_id")
                if bound_run_id and int(bound_run_id) != run_id:
                    if stage.get("status") != "failed" or run_id <= int(bound_run_id):
                        continue
                next_status = result.get("status")
                attempt = int(result.get("run_attempt") or stage.get("run_attempt") or 1)
                if not next_status or (stage.get("status") == next_status and stage.get("run_attempt") == attempt and stage.get("run_id") == run_id):
                    continue
                stage.update({"run_id": run_id, "status": next_status, "run_attempt": attempt})
                task[f"{stage_name}_run_id"] = run_id
                if next_status == "completed":
                    stage.update({"reason": None, "completed_at": result.get("updated_at")})
                elif next_status == "failed":
                    stage["reason"] = result.get("conclusion") or "failure"
                changed = True
            if task.get("status") == "canceling":
                continue
            next_task_status, reason = preflight_status(stages)
            if task.get("status") != next_task_status or task.get("reason") != reason:
                task.update(status=next_task_status, reason=reason)
                changed = True
        return changed

    @staticmethod
    def _observe_preflight_stage(repo, token, run_id, artifact_prefix):
        headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(
            f"https://api.github.com/repos/{repo}/actions/runs/{int(run_id)}",
            headers=headers, timeout=15,
        )
        response.raise_for_status()
        run = response.json()
        if run.get("status") != "completed":
            return {"status": "running", "conclusion": None, "updated_at": run.get("updated_at"), "run_attempt": int(run.get("run_attempt") or 1)}
        conclusion = run.get("conclusion")
        if conclusion != "success":
            return {"status": "failed", "conclusion": conclusion, "updated_at": run.get("updated_at"), "run_attempt": int(run.get("run_attempt") or 1)}
        artifacts = requests.get(
            f"https://api.github.com/repos/{repo}/actions/runs/{int(run_id)}/artifacts",
            headers=headers, params={"per_page": 100}, timeout=15,
        )
        artifacts.raise_for_status()
        artifact_ready = any(
            not item.get("expired") and str(item.get("name") or "").startswith(artifact_prefix)
            for item in artifacts.json().get("artifacts", [])
        )
        return {
            "status": "completed" if artifact_ready else "running",
            "run_attempt": int(run.get("run_attempt") or 1),
            "conclusion": conclusion,
            "updated_at": run.get("updated_at"),
        }

    @staticmethod
    def _discover_preflight_run(repo, token, workflow_file, task_id, dispatched_at=None):
        """Recover a Run ID lost during workflow_dispatch indexing or a later re-run."""
        headers = {
            "Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs",
            headers=headers, params={"event": "workflow_dispatch", "per_page": 100}, timeout=15,
        )
        response.raise_for_status()
        since = None
        if dispatched_at:
            try:
                since = datetime.fromisoformat(str(dispatched_at).replace("Z", "+00:00"))
            except ValueError:
                pass
        matches = []
        for run in response.json().get("workflow_runs", []):
            name = run.get("display_title") or run.get("name")
            if task_id_from_run_name(name) != task_id:
                continue
            if since:
                try:
                    created = datetime.fromisoformat(str(run.get("created_at") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if created < since.replace(microsecond=0):
                    continue
            matches.append(run)
        return max(matches, key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)), default=None)

