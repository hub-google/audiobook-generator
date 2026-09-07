import base64
import json
from pathlib import Path

import pytest

from src.book_profiles import book_profile_id
from src.cover_preflight import build_cover_config
from src.source_identity import source_fingerprint


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "cover-preflight.yml"


def encoded(value):
    return base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")


def test_cover_config_uses_dispatch_identity_without_catalog_data():
    url = "https://www.69shuba.com/book/90225/"
    snapshot = {
        "book_profile_id": book_profile_id(url),
        "catalog_url": url,
        "profile_revision": 3,
        "manual_cover": {"enabled": False},
    }

    config = build_cover_config("測試小說", url, encoded(snapshot))

    assert config["book_title"] == "測試小說"
    assert config["source_fingerprint"] == source_fingerprint(url)
    assert config["book_profile_id"] == book_profile_id(url)
    assert config["profile_revision"] == 3
    assert config["cover_catalog_lookup"] is False
    assert "catalog_snapshot" not in config
    assert "chapters" not in config


def test_cover_config_rejects_snapshot_from_another_book():
    with pytest.raises(ValueError, match="另一個來源"):
        build_cover_config(
            "測試小說",
            "https://www.69shuba.com/book/90225/",
            encoded({"catalog_url": "https://www.69shuba.com/book/1/"}),
        )


def test_cover_workflow_cannot_depend_on_catalog_scraping():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "src/cover_preflight.py" in workflow
    assert "--book-profile-snapshot-b64" in workflow
    assert "catalog_parser.py" not in workflow
    assert "crawler.py" not in workflow
    assert "matrix.json" not in workflow
