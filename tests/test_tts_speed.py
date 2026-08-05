import asyncio
import inspect
import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from src.catalog_parser import generate_config_yaml
from src.tts_ms import _generate_one_segment


class TTSSpeedTests(unittest.TestCase):
    def test_legacy_jobs_keep_original_speed_by_default(self):
        parameters = inspect.signature(_generate_one_segment).parameters
        self.assertEqual(parameters["rate"].default, "+0%")

    def test_generated_config_uses_one_point_five_speed(self):
        parsed = {
            "success": True,
            "book_title": "測試小說",
            "base_url": "https://example.com",
            "chapters": ["/chapter/1"],
            "chapter_titles": ["第一章"],
            "total_chapters": 1,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "config.yaml")
            generate_config_yaml(
                "https://example.com/catalog",
                output_path=output_path,
                parsed_result=parsed,
            )
            with open(output_path, encoding="utf-8") as config_file:
                config = yaml.safe_load(config_file)

        self.assertEqual(config["tts"]["edge_rate"], "+50%")

    def test_edge_tts_receives_configured_rate(self):
        calls = []

        class FakeCommunicate:
            def __init__(self, text, voice, *, rate):
                calls.append((text, voice, rate))

            async def save(self, path):
                with open(path, "wb") as output_file:
                    output_file.write(b"x" * 101)

        with tempfile.TemporaryDirectory() as temp_dir:
            mp3_path = os.path.join(temp_dir, "part.mp3")
            wav_path = os.path.join(temp_dir, "part.wav")
            with patch("src.tts_ms.edge_tts.Communicate", FakeCommunicate):
                result = asyncio.run(
                    _generate_one_segment(
                        asyncio.Semaphore(1),
                        "測試文字",
                        mp3_path,
                        wav_path,
                        "test-part",
                        "zh-CN-YunxiNeural",
                        rate="+50%",
                        max_retries=1,
                    )
                )

        self.assertTrue(result)
        self.assertEqual(calls, [("測試文字", "zh-CN-YunxiNeural", "+50%")])


if __name__ == "__main__":
    unittest.main()
