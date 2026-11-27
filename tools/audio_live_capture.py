"""Mirror active Python worker console lines to the private AUDIO live journal.

GitHub Actions still receives the original stdout/stderr. This wrapper never
puts repository/API secrets in Pages and never fails the original worker merely
because the status journal is unavailable.
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import requests

EDGE = "https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-web-commands"
SENSITIVE = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*)[^\s&]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(?:gh[pousr]_|hf_)[A-Za-z0-9]{12,}\b"), "[REDACTED]"),
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
    (re.compile(r"\x1b\[[0-9;]*m"), ""),
)


def sanitize(line):
    text = line.strip().replace("\r", " ")
    for pattern, replacement in SENSITIVE:
        text = pattern.sub(replacement, text)
    return text[:800]


def severity(line):
    if re.search(r"\b(?:ERROR|CRITICAL|Traceback|Exception|FAILED)\b", line, re.I):
        return "error"
    if re.search(r"\b(?:WARN|WARNING|RETRY)\b", line, re.I):
        return "warning"
    return "info"


class LiveJournal:
    def __init__(self, task_id, job_name):
        self.task_id = task_id
        self.job_name = re.sub(r"[^A-Za-z0-9_-]", "_", job_name)[:90] or "worker"
        self.run_id = os.environ.get("GITHUB_RUN_ID") or ""
        self.pending = queue.Queue(maxsize=1000)
        self.finish = threading.Event()
        self.thread = None
        self.jwt = None
        self.jwt_at = 0.0
        self.available = bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
                              and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
                              and re.fullmatch(r"book-[A-Za-z0-9_-]{5,80}", task_id)
                              and self.run_id.isdigit())

    def auth(self):
        if self.jwt and time.monotonic() - self.jwt_at < 180:
            return self.jwt
        url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        url += ("&" if "?" in url else "?") + urlencode({"audience": "audio-web-commands"})
        response = requests.get(
            url,
            headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
            timeout=(5, 12),
        )
        response.raise_for_status()
        self.jwt = response.json()["value"]
        self.jwt_at = time.monotonic()
        return self.jwt

    def write(self, line):
        if not self.available:
            return
        message = sanitize(line)
        if not message:
            return
        try:
            self.pending.put_nowait({"message": message, "level": severity(line)})
        except queue.Full:
            # The real GitHub console remains authoritative even if journal
            # storage/network cannot keep pace. Never block TTS or crawling.
            pass

    def start(self):
        if self.available:
            self.thread = threading.Thread(target=self._pump, name="audio-live-journal", daemon=True)
            self.thread.start()

    def _pump(self):
        while not self.finish.is_set() or not self.pending.empty():
            batch = []
            try:
                batch.append(self.pending.get(timeout=2 if not self.finish.is_set() else 0.05))
            except queue.Empty:
                continue
            while len(batch) < 30:
                try:
                    batch.append(self.pending.get_nowait())
                except queue.Empty:
                    break
            try:
                response = requests.post(
                    EDGE + "/live_append",
                    json={"task_id": self.task_id, "run_id": int(self.run_id),
                          "job_name": self.job_name, "lines": batch},
                    headers={"Authorization": "Bearer " + self.auth()},
                    timeout=(5, 12),
                )
                response.raise_for_status()
            except (requests.RequestException, KeyError, ValueError):
                # No retries here: if the journal is down, do not retry
                # indefinitely or hold the primary production job open.
                if self.finish.is_set():
                    break

    def close(self):
        self.finish.set()
        if self.thread:
            self.thread.join(timeout=6)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("command to execute is required")
    journal = LiveJournal(args.task_id, args.job)
    journal.start()
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            journal.write(line)
        return process.wait()
    finally:
        journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
