import json
import os
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch
import asyncio

from src.artifact_validation import (
    ArtifactRegistry, ArtifactValidationError, stable_signature, validate_srt,
)
from src.part_builder import validate_chapter_continuity
from src.publication_checkpoint import PublicationCheckpoint
from src.tts_ms import _generate_one_segment, segment_cache_key
from src.youtube_api_uploader import add_video_to_playlist, upload_video_file


class ResumeArchitectureTests(unittest.TestCase):
    def test_registry_does_not_repeat_expensive_validation_for_unchanged_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, "large.mp4")
            with open(artifact, "wb") as handle:
                handle.write(b"immutable video bytes")
            validator = Mock(return_value={"sha256": "a" * 64, "duration_seconds": 10})
            registry = ArtifactRegistry(os.path.join(directory, "registry.json"))
            kwargs = dict(validator_key="video-v1", input_signature="in", settings_signature="cfg")
            registry.validate(artifact, validator, **kwargs)
            registry.validate(artifact, validator, **kwargs)
            self.assertEqual(validator.call_count, 1)
            with open(artifact, "ab") as handle:
                handle.write(b"changed")
            registry.validate(artifact, validator, **kwargs)
            self.assertEqual(validator.call_count, 2)

    def test_srt_content_coverage_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "chapter.srt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\n第一段\n\n")
            with self.assertRaises(ArtifactValidationError):
                validate_srt(path, 1.0, expected_text="第一段，第二段。")

    def test_tts_cache_key_depends_on_content_and_voice_settings(self):
        base = segment_cache_key("同一句", "voice-a", "+0%", "cfg")
        self.assertEqual(base, segment_cache_key("同一句", "voice-a", "+0%", "cfg"))
        self.assertNotEqual(base, segment_cache_key("前面新增一句", "voice-a", "+0%", "cfg"))
        self.assertNotEqual(base, segment_cache_key("同一句", "voice-b", "+0%", "cfg"))
        self.assertNotEqual(base, segment_cache_key("同一句", "voice-a", "+25%", "cfg"))

    def test_tts_bounded_chain_records_silent_fallback(self):
        records = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("src.tts_ms.edge_tts.Communicate", side_effect=RuntimeError("policy")), \
                patch("src.tts_ms.asyncio.sleep"), \
                patch("src.tts_ms.create_silent_wav", return_value=True):
            result = asyncio.run(_generate_one_segment(
                asyncio.Semaphore(1), "禁詞片段", os.path.join(directory, "x.mp3"),
                os.path.join(directory, "x.wav"), "segment", "voice", max_retries=2,
                normalized_retries=1, split_retries=1, fallback_records=records,
                chapter=7, segment_index=3,
            ))
        self.assertTrue(result)
        self.assertEqual(records[0]["chapter"], 7)
        self.assertEqual(records[0]["segment_index"], 3)
        self.assertTrue(records[0]["silent_fallback_used"])
        self.assertEqual(len(records[0]["original_text_hash"]), 64)

    def test_part_gap_requires_confirmed_source_missing(self):
        with self.assertRaises(ArtifactValidationError):
            validate_chapter_continuity([1, 2, 4])
        self.assertTrue(validate_chapter_continuity([1, 2, 4], {3}))

    def test_publication_write_ack_is_nested_and_crash_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = PublicationCheckpoint(os.path.join(directory, "resume.json"))
            checkpoint.lock_plan([{"part_num": 1, "start_chap": 1, "end_chap": 2,
                                   "chapters": [1, 2], "duration": 20, "title": "Part 1"}])
            checkpoint.record_upload_ack(1, "video-1", "b" * 64)
            checkpoint.record_thumbnail_ack(1)
            restored = PublicationCheckpoint(os.path.join(directory, "resume.json"))
            part = restored.data["parts"]["1"]
            self.assertEqual(part["upload"]["video_id"], "video-1")
            self.assertEqual(part["thumbnail"]["status"], "completed")
            self.assertEqual(part["playlist"]["status"], "pending")

    def test_playlist_insert_returns_server_ack_without_list(self):
        request = Mock()
        request.execute.return_value = {"id": "playlist-item-1"}
        resource = Mock()
        resource.insert.return_value = request
        youtube = Mock()
        youtube.playlistItems.return_value = resource
        self.assertEqual(add_video_to_playlist(youtube, "pl", "vid", 0), "playlist-item-1")
        self.assertFalse(resource.list.called)


if __name__ == "__main__":
    unittest.main()
