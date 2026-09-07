"""Build a JSON advertisement-candidate report from normalized RawText files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

from raw_text_normalizer import RAW_NORMALIZER_VERSION


def _load_detector():
    from ad_detection import NovelAdDetector
    return NovelAdDetector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker-id", required=True, type=int)
    parser.add_argument("--config")
    args = parser.parse_args()
    raw_dir = Path(args.raw_dir)
    files = sorted(raw_dir.glob("*_raw.txt"))
    if args.config:
        import yaml
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
        actual = {int(re.search(r"chapter_(\d+)_raw", p.name).group(1)) for p in files}
        if actual != set(config["selected_indices"]):
            raise RuntimeError("TXT chapter inventory differs from selected catalog; review blocked")
    if not files:
        raise SystemExit(f"No RawText files in {raw_dir}")
    detector = _load_detector()(min_count=2)
    results = detector.analyze_source(raw_dir)
    chapters = []
    combined = hashlib.sha256()
    texts = {}
    for path in files:
        match = re.search(r"chapter_(\d+)_raw", path.name)
        number = int(match.group(1)) if match else None
        raw = path.read_bytes()
        combined.update(path.name.encode("utf-8")); combined.update(raw)
        texts[number] = raw.decode("utf-8")
        chapters.append(number)
    candidates = []
    for item in results:
        affected = [number for number, text in texts.items() if item.text in text]
        candidates.append({
            "text": item.text, "kind": item.kind, "count": item.count,
            "spread": item.spread, "score": item.score, "reason": item.reason,
            "suggested_remove": bool(item.enabled), "affected_chapters": affected,
            "samples": item.samples,
        })
    payload = {
        "schema_version": 1, "artifact_role": "ad_review_candidates",
        "task_id": args.task_id, "worker_id": args.worker_id,
        "raw_normalizer_version": RAW_NORMALIZER_VERSION,
        "raw_fingerprint": combined.hexdigest(), "chapters": chapters,
        "candidates": candidates,
        "chapter_texts": texts,
    }
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
