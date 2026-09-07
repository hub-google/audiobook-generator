"""Restore reviewed TXT catalog identity and apply the approved cleaner snapshot."""
import base64
import json
import os
from pathlib import Path
import yaml


def main():
    config = yaml.safe_load(Path("scrape_config/config.yaml").read_text(encoding="utf-8"))
    snapshot = json.loads(base64.b64decode(os.environ["PROFILE_SNAPSHOT"]))
    if snapshot.get("book_profile_id") != config.get("book_profile_id"):
        raise RuntimeError("Reviewed TXT belongs to a different book")
    config["cleaner"] = {"remove_patterns": snapshot["cleaner_remove_patterns"],
                         "fingerprint": snapshot["cleaner_fingerprint"]}
    config["profile_revision"] = snapshot["profile_revision"]
    config["manual_cover"] = snapshot.get("manual_cover") or {}
    Path("config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    Path("matrix.json").write_bytes(Path("scrape_config/matrix.json").read_bytes())


if __name__ == "__main__":
    main()
