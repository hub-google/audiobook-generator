import base64
import json
from pathlib import Path

import pytest
from unittest.mock import patch

from src.book_profiles import book_profile_id
from src.cover_preflight import build_cover_config
from src.metadata_gen import auto_generate_prompt_from_summary
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


def test_cover_preflight_reaches_gemini_without_fetching_a_synopsis(monkeypatch):
    monkeypatch.setenv("COVER_GEMINI_TITLE_ONLY", "1")
    visual_brief = {
        "genre": "cultivation fantasy",
        "era_and_setting": "demonic cultivation world",
        "core_conflict": "survival through strategic inaction",
        "main_character_identity": "a reluctant cultivator",
        "appearance": "handsome young man",
        "clothing": "dark cultivation robes",
        "expression_and_action": "calmly observing the conflict",
        "supporting_characters": [],
        "iconic_story_symbol": "a colossal demonic citadel",
        "iconic_prop_or_power": "swirling demonic energy",
        "genre_color_palette": "crimson and black",
        "lighting_and_mood": "dramatic cinematic lighting",
        "avoid_story_errors": ["modern technology"],
    }
    gemini_result = {
        "analysis": {"世界觀": "天魔世界"},
        "story_facts": [{"fact": "模型辨識出的故事事實", "source_ids": ["MODEL_KNOWLEDGE"]}] * 5,
        "visual_brief": visual_brief,
        "template_version": "test",
        "prompt": "unused",
    }

    with patch("src.metadata_gen.fetch_book_summary_details") as fetch, \
         patch("src.metadata_gen.collect_cover_research") as research, \
         patch("src.metadata_gen.generate_gemini_cover_information", return_value=gemini_result) as gemini, \
         patch("src.metadata_gen.review_cover_information", return_value=gemini_result):
        synopsis, _, _, _ = auto_generate_prompt_from_summary("在天魔世界的摆烂生活")

    fetch.assert_not_called()
    research.assert_not_called()
    assert "在天魔世界的摆烂生活" in synopsis
    assert gemini.call_args.kwargs["research"] == {"mode": "internal_knowledge_fallback", "sources": []}
