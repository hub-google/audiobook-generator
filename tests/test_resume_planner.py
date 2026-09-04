import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.artifact_validation import ArtifactValidationError
from src.part_matrix import build_merge_matrix
from src.publication_checkpoint import plan_structure_fingerprint
from src.resume_planner import (
    TransientRemoteValidationError,
    list_candidate_runs,
    list_run_artifacts,
    validate_merge_shard,
    verify_hf_video,
    config_fingerprint,
)


def _completed(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_list_run_artifacts_uses_supported_paginated_json_lines_and_deduplicates():
    artifacts = [{"id": i, "name": f"item-{i}", "expired": False} for i in range(1, 151)]
    artifacts += [{"id": 151, "name": "item-1", "expired": False},
                  {"id": 152, "name": "expired", "expired": True}]
    runner = Mock(return_value=_completed("\n".join(json.dumps(x) for x in artifacts)))
    result = list_run_artifacts("owner/repo", "42", runner=runner)
    command = runner.call_args.args[0]
    assert command[1:3] == ["api", "--paginate"]
    assert "--slurp" not in command
    assert command[command.index("--jq") + 1] == ".artifacts[] | @json"
    assert len(result) == 150
    assert result["item-1"]["id"] == 151
    assert "expired" not in result


def test_list_run_artifacts_api_failure_is_fatal():
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="cannot list artifacts"):
        list_run_artifacts("owner/repo", "42", runner=runner)


def test_candidate_runs_paginate_exclude_current_and_explicit_is_locked():
    runner = Mock(return_value=_completed("99\n100\n101\n"))
    assert list_candidate_runs("owner/repo", "100", runner=runner) == ["99", "101"]
    command = runner.call_args.args[0]
    assert "--paginate" in command and "--slurp" not in command
    runner.reset_mock()
    assert list_candidate_runs("owner/repo", "100", explicit="77", runner=runner) == ["77"]
    runner.assert_not_called()


def test_candidate_runs_api_failure_is_fatal():
    runner = Mock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="cannot discover"):
        list_candidate_runs("owner/repo", "100", runner=runner)


def test_media_fingerprint_tracks_source_voice_and_video_but_not_publication_metadata():
    base = {"book_title": "Book", "catalog_url": "https://example/book", "selected_indices": [1],
            "source_indices": [2], "chapters": ["https://example/ch/2"], "chapter_titles": ["Ch 1"],
            "chapter_order": [2], "cleaner": {"fingerprint": "clean"},
            "tts": {"edge_voice": "voice-a", "edge_rate": "+25%"}, "video": {"fps": 30},
            "youtube_description": "old"}
    for path, value in [("catalog_url", "https://other/book"), ("source_indices", [3]),
                        ("chapters", ["https://other/ch/2"]), ("chapter_titles", ["Changed"]),
                        ("tts", {"edge_voice": "voice-b"}), ("video", {"fps": 60})]:
        changed = dict(base); changed[path] = value
        assert config_fingerprint(changed) != config_fingerprint(base)
    publication_only = dict(base); publication_only["youtube_description"] = "new"
    assert config_fingerprint(publication_only) == config_fingerprint(base)


def test_part_structure_fingerprint_ignores_title_only():
    base = [{"part_num": 1, "start_chap": 1, "end_chap": 2, "chapters": [1, 2], "title": ""}]
    titled = [{**base[0], "title": "YouTube display title"}]
    changed = [{**base[0], "chapters": [1, 3], "end_chap": 3}]
    assert plan_structure_fingerprint(base) == plan_structure_fingerprint(titled)
    assert plan_structure_fingerprint(base) != plan_structure_fingerprint(changed)


@pytest.mark.parametrize("count", [1, 17, 18, 50])
def test_merge_matrix_is_capped_complete_unique_and_stable(count):
    numbers = list(range(1, count + 1))
    matrix = build_merge_matrix(numbers)
    assert len(matrix["include"]) <= 17
    workers = [[int(x) for x in item["part_numbers"].split(",")] for item in matrix["include"]]
    flattened = [worker[row] for row in range(max(map(len, workers)))
                 for worker in workers if row < len(worker)]
    assert flattened == numbers
    assert sorted(number for worker in workers for number in worker) == numbers


def _merge_fixture(tmp_path, video=b"remote-video-bytes"):
    subtitle = tmp_path / "part-1.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n", encoding="utf-8")
    part = {"part_num": 1, "chapters": [1], "duration": 1.0, "subtitle": subtitle.name,
            "subtitle_bytes": subtitle.stat().st_size,
            "subtitle_sha256": hashlib.sha256(subtitle.read_bytes()).hexdigest(),
            "video_bytes": len(video), "video_sha256": hashlib.sha256(video).hexdigest(),
            "hf_video_path": "books/run/part-1.mp4"}
    (tmp_path / "shard-manifest-1.json").write_text(json.dumps({
        "source_run_id": "7", "parts": [part]
    }), encoding="utf-8")
    return {"source_run_id": "7", "parts": [{"part_num": 1, "chapters": [1]}]}, part, video


def test_merge_shard_reuses_only_when_remote_bytes_are_verified(tmp_path):
    plan, _, _ = _merge_fixture(tmp_path)
    verifier = Mock()
    assert set(validate_merge_shard(tmp_path, plan, remote_verifier=verifier)) == {1}
    verifier.assert_called_once()


@pytest.mark.parametrize("message", ["missing", "size mismatch", "hash mismatch"])
def test_merge_shard_rejects_missing_or_corrupt_remote_bytes(tmp_path, message):
    plan, _, _ = _merge_fixture(tmp_path)
    with pytest.raises(ArtifactValidationError, match=message):
        validate_merge_shard(tmp_path, plan, remote_verifier=Mock(
            side_effect=ArtifactValidationError(message)))


def test_merge_shard_preserves_transient_remote_semantics(tmp_path):
    plan, _, _ = _merge_fixture(tmp_path)
    with pytest.raises(TransientRemoteValidationError):
        validate_merge_shard(tmp_path, plan, remote_verifier=Mock(
            side_effect=TransientRemoteValidationError("network")))


def test_verify_hf_video_checks_size_and_sha(tmp_path):
    plan, part, video = _merge_fixture(tmp_path)
    cached = tmp_path / "cached.mp4"
    cached.write_bytes(video)
    with patch("huggingface_hub.hf_hub_download", return_value=str(cached)) as download:
        assert verify_hf_video(part, token="token", repo_id="owner/archive") == cached
        assert download.call_args.kwargs["repo_type"] == "dataset"
    cached.write_bytes(video + b"bad")
    with patch("huggingface_hub.hf_hub_download", return_value=str(cached)):
        with pytest.raises(ArtifactValidationError, match="size mismatch"):
            verify_hf_video(part, token="token", repo_id="owner/archive")
    cached.write_bytes(b"X" * len(video))
    with patch("huggingface_hub.hf_hub_download", return_value=str(cached)):
        with pytest.raises(ArtifactValidationError, match="hash mismatch"):
            verify_hf_video(part, token="token", repo_id="owner/archive")


def test_verify_hf_video_network_failure_is_transient():
    part = {"hf_video_path": "x", "video_bytes": 1, "video_sha256": "0" * 64}
    with patch("huggingface_hub.hf_hub_download", side_effect=TimeoutError("network")):
        with pytest.raises(TransientRemoteValidationError):
            verify_hf_video(part, token="token", repo_id="owner/archive")
