import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from unittest.mock import patch

from src.book_profiles import book_profile_id
from src.cover_preflight import (
    _run_introduction_stage,
    _run_cover_information_stage,
    _run_cover_stage,
    _run_review_stage,
    build_cover_config,
)
from src.metadata_gen import auto_generate_prompt_from_summary
from src.source_identity import source_fingerprint


WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "cover-preflight.yml"
AUDIOBOOK_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "audiobook.yml"


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


def test_cover_workflow_splits_existing_paid_calls_into_four_jobs():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "generate_book_introduction:" in workflow
    assert "generate_cover_information:" in workflow
    assert "review_cover_information:" in workflow
    assert "generate_cover:" in workflow
    assert "needs: generate_book_introduction" in workflow
    assert "needs: generate_cover_information" in workflow
    assert "needs: review_cover_information" in workflow
    assert workflow.count("--stage introduction") == 1
    assert workflow.count("--stage cover-information") == 1
    assert workflow.count("--stage review") == 1
    assert workflow.count("--stage cover \\") == 1


def test_cover_information_stage_only_calls_existing_draft_generator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"book_title": "測試小說", "book_profile_id": "book-id", "paths": {"workspace_base": "Workspace"}}
    cover_dir = tmp_path / "Workspace" / config["book_title"] / "Cover"
    cover_dir.mkdir(parents=True)
    (cover_dir / "book_introduction.json").write_text(
        json.dumps({"manual_cover": False, "introduction": "足夠長的測試小說故事介紹內容"}), encoding="utf-8",
    )
    draft = {"status": "ok", "prompt": "draft prompt"}

    with patch("src.cover_preflight.generate_gemini_cover_information", return_value=draft) as generate, \
         patch("src.cover_preflight.generate_gemini_book_introduction") as introduce, \
         patch("src.cover_preflight.review_cover_information") as review, \
         patch("src.cover_preflight.download_ai_image") as image:
        _run_cover_information_stage(config)

    generate.assert_called_once()
    introduce.assert_not_called()
    review.assert_not_called()
    image.assert_not_called()


def test_introduction_stage_only_calls_existing_introduction_generator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(
        book_title="測試小說",
        catalog_url="https://example.test/book/1/",
        book_profile_snapshot_b64="",
    )
    config_path = tmp_path / "config.yaml"

    with patch("src.cover_assets.restore_from_config") as restore, \
         patch("src.cover_preflight.generate_gemini_book_introduction", return_value="足夠長的小說故事介紹") as introduce, \
         patch("src.cover_preflight.generate_gemini_cover_information") as generate, \
         patch("src.cover_preflight.review_cover_information") as review, \
         patch("src.cover_preflight.download_ai_image") as image:
        _run_introduction_stage(args, config_path)

    restore.assert_not_called()
    introduce.assert_called_once_with("測試小說")
    generate.assert_not_called()
    review.assert_not_called()
    image.assert_not_called()


def test_review_stage_does_not_repeat_introduction_or_draft(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"book_title": "測試小說", "book_profile_id": "book-id", "paths": {"workspace_base": "Workspace"}}
    cover_dir = tmp_path / "Workspace" / config["book_title"] / "Cover"
    cover_dir.mkdir(parents=True)
    (cover_dir / "book_introduction.json").write_text(
        json.dumps({"manual_cover": False, "introduction": "足夠長的測試小說故事介紹內容"}), encoding="utf-8",
    )
    (cover_dir / "cover_information_draft.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    reviewed = {
        "analysis": {}, "story_facts": [], "visual_brief": {},
        "template_version": "test", "prompt": "final prompt",
    }

    with patch("src.cover_preflight.review_cover_information", return_value=reviewed) as review, \
         patch("src.cover_preflight.generate_gemini_book_introduction") as introduce, \
         patch("src.cover_preflight.generate_gemini_cover_information") as generate, \
         patch("src.cover_preflight.download_ai_image") as image:
        _run_review_stage(config)

    review.assert_called_once()
    introduce.assert_not_called()
    generate.assert_not_called()
    image.assert_not_called()


def test_cover_stage_only_calls_existing_hf_generator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"book_title": "測試小說", "book_profile_id": "book-id", "paths": {"workspace_base": "Workspace"}}
    cover_dir = tmp_path / "Workspace" / config["book_title"] / "Cover"
    cover_dir.mkdir(parents=True)
    (cover_dir / "book_introduction.json").write_text(
        json.dumps({"manual_cover": False, "introduction": "足夠長的測試小說故事介紹內容"}), encoding="utf-8",
    )
    (cover_dir / "master_cover_prompt.json").write_text(
        json.dumps({"prompt": "final prompt"}), encoding="utf-8",
    )
    args = SimpleNamespace(task_id="task-1")

    with patch("src.cover_preflight.download_ai_image", return_value=Image.new("RGB", (1280, 720))) as image, \
         patch("src.cover_preflight.save_process_log"), \
         patch("src.cover_preflight.generate_gemini_book_introduction") as introduce, \
         patch("src.cover_preflight.generate_gemini_cover_information") as generate, \
         patch("src.cover_preflight.review_cover_information") as review:
        _run_cover_stage(config, args)

    image.assert_called_once_with("final prompt", width=1280, height=720)
    introduce.assert_not_called()
    generate.assert_not_called()
    review.assert_not_called()
    assert (cover_dir / "master_cover.jpg").is_file()
    assert (cover_dir / "cover-preflight-manifest.json").is_file()


def test_processing_applies_manual_cover_after_the_reviewed_artifact():
    workflow = AUDIOBOOK_WORKFLOW_PATH.read_text(encoding="utf-8")

    download = workflow.index("- name: Download approved preflight cover")
    restore = workflow.index("- name: Restore and validate approved cover", download)
    manual = workflow.index("- name: Apply verified manual cover override when configured", restore)

    assert download < restore < manual
    assert "python src/cover_assets.py --restore-config prepared_source/config.yaml" in workflow[manual:]


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

    introduction = "書名：《在天魔世界的摆烂生活》；故事簡介：主角身處天魔世界，面對宗門與生存衝突，以看似擺爛的方式周旋並逐步改變命運。"
    with patch("src.metadata_gen.fetch_book_summary_details") as fetch, \
         patch("src.metadata_gen.collect_cover_research") as research, \
         patch("src.metadata_gen.generate_gemini_book_introduction", return_value=introduction) as introduce, \
         patch("src.metadata_gen.generate_gemini_cover_information", return_value=gemini_result) as gemini, \
         patch("src.metadata_gen.review_cover_information", return_value=gemini_result):
        synopsis, _, _, _ = auto_generate_prompt_from_summary("在天魔世界的摆烂生活")

    fetch.assert_not_called()
    research.assert_not_called()
    introduce.assert_called_once_with("在天魔世界的摆烂生活")
    assert "在天魔世界的摆烂生活" in synopsis
    assert gemini.call_args.kwargs["research"] == {"mode": "internal_knowledge_fallback", "sources": []}
