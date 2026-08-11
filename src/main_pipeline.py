"""Strict local entry point shared with the GitHub Actions worker pipeline."""

import json
import logging
import os
import sys

import yaml

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(SRC_DIR, ".."))
sys.path.insert(0, SRC_DIR)

from worker_pipeline import run_pipeline


def load_config(config_path=None):
    path = config_path or os.path.join(PROJECT_DIR, "config.yaml")
    with open(path, "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def setup_logger(workspace_dir):
    os.makedirs(workspace_dir, exist_ok=True)
    root_logger = logging.getLogger()
    if root_logger.handlers:
        root_logger.handlers.clear()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(workspace_dir, "system.log"), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def write_run_summary(checkpoint, status, error=None):
    """Persist human- and machine-readable truth for GUI and operators."""
    summary_dir = os.path.join(checkpoint.workspace_dir, "RunStatus")
    os.makedirs(summary_dir, exist_ok=True)
    markdown = checkpoint.markdown_summary()
    if error:
        markdown += f"\n## Local runner result\n\n- Status: **{status}**\n- Error: {error}\n"
    else:
        markdown += f"\n## Local runner result\n\n- Status: **{status}**\n"
    markdown_path = os.path.join(summary_dir, "run-summary.md")
    json_path = os.path.join(summary_dir, "run-summary.json")
    with open(markdown_path + ".tmp", "w", encoding="utf-8") as output:
        output.write(markdown)
        output.flush()
        os.fsync(output.fileno())
    os.replace(markdown_path + ".tmp", markdown_path)
    payload = {
        "status": status,
        "error": str(error) if error else None,
        "checkpoint": checkpoint.data,
    }
    with open(json_path + ".tmp", "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(json_path + ".tmp", json_path)


def main(config_path=None):
    config = load_config(config_path)
    book_title = config.get("book_title", "UnknownBook")
    workspace_dir = os.path.abspath(os.path.join(
        PROJECT_DIR, config["paths"]["workspace_base"], book_title
    ))
    setup_logger(workspace_dir)
    logging.info("=== Strict Local Audiobook Pipeline Started ===")
    logging.info("[Info] Book: %s", book_title)

    checkpoint = None
    try:
        checkpoint = run_pipeline(
            config,
            worker_id=0,
            chapters=config.get("chapters"),
            exact_indices=config.get("selected_indices"),
            build_parts=True,
        )
        write_run_summary(checkpoint, "SUCCESS")
    except Exception as error:
        logging.exception("=== Local Pipeline FAILED: %s ===", error)
        if checkpoint is None:
            from pipeline_checkpoint import PipelineCheckpoint
            checkpoint = PipelineCheckpoint(
                workspace_dir, book_title, 0, config.get("selected_indices") or []
            )
        write_run_summary(checkpoint, "FAILED", error)
        raise

    logging.info("=== Local Pipeline SUCCESS ===")
    logging.info("[Output] Workspace: %s", workspace_dir)
    return checkpoint


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
