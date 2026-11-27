"""Run the same metadata_gen.build_cover_information as Tkinter batch GUI."""
from __future__ import annotations
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.cloud_queue import GitHubQueueStore
from src.metadata_gen import build_cover_information

TASK_PATTERN = re.compile(r"^book-\d{8}-[0-9a-f]{8}$")


def parse_task_ids(value):
    ids = (value or "").split(",")
    if not ids or any(not TASK_PATTERN.fullmatch(item) for item in ids):
        raise ValueError("Invalid AUDIO book task identifiers")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate book task ID")
    return ids


def run_batch(tasks, generator=build_cover_information):
    """No invented concurrency limit: desktop uses max_workers=len(tasks)."""
    if not tasks:
        raise ValueError("No books selected")
    output = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        future_to_task = {
            pool.submit(generator, task.get("book_title") or "待解析",
                        task.get("catalog_url") or ""): task
            for task in tasks
        }
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            try:
                data = future.result()
                if not isinstance(data, dict) or not data.get("prompt"):
                    raise RuntimeError("Cover information lacks reviewed image Prompt")
                output[task["task_id"]] = {"status": "completed", "data": data}
            except Exception as error:
                # Do not accidentally publish API keys embedded in exception text.
                output[task["task_id"]] = {"status": "failed", "error": type(error).__name__}
    return [{
        "task_id": task["task_id"], "book_title": task.get("book_title") or "待解析",
        **output[task["task_id"]],
    } for task in tasks]


def main():
    ids = parse_task_ids(os.environ["SELECTED_TASK_IDS"])
    request_id = os.environ.get("REQUEST_ID", "")
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("Invalid batch cover request ID")
    queue, _ = GitHubQueueStore(
        os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"],
    ).load()
    task_map = {
        task["task_id"]: task for task in
        queue.get("queue", []) + queue.get("completed", [])
    }
    if set(ids) - set(task_map):
        raise ValueError("Selected audiobook tasks changed since authorization")
    data = {
        "schema_version": 1, "role": "audio_batch_cover_information",
        "request_id": request_id,
        "results": run_batch([task_map[key] for key in ids]),
    }
    Path("batch-cover-info.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    for row in data["results"]:
        print(f"[BATCH_COVER] {row['task_id']} {row['status']}", flush=True)


if __name__ == "__main__":
    main()
