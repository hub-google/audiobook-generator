import json
import os
import tempfile
import unittest
import wave
from unittest.mock import patch

from PIL import Image

from src.artifact_validation import (
    ArtifactValidationError, validate_image, validate_srt, validate_text,
    validate_video, validate_wav,
)


class ArtifactValidationTests(unittest.TestCase):
    def test_validates_real_text_wav_srt_and_image_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = os.path.join(directory, "raw.txt")
            clean = os.path.join(directory, "clean.txt")
            wav_path = os.path.join(directory, "audio.wav")
            srt = os.path.join(directory, "subtitle.srt")
            image = os.path.join(directory, "image.jpg")
            with open(raw, "w", encoding="utf-8") as handle:
                handle.write("第一章 測試\n\n這是一段足夠長的小說正文內容，用來確認抓取結果不是空白錯誤頁面。")
            with open(clean, "w", encoding="utf-8") as handle:
                handle.write("這是一段清理完成而且可以交給語音引擎處理的小說正文內容。")
            with wave.open(wav_path, "wb") as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
                audio.writeframes(b"\0\0" * 24000)
            with open(srt, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:00,000 --> 00:00:01,000\n測試字幕\n")
            Image.new("RGB", (1280, 720), "navy").save(image, "JPEG")

            self.assertIn("sha256", validate_text(raw))
            self.assertIn("sha256", validate_text(clean, clean=True))
            self.assertEqual(validate_wav(wav_path)["sample_rate"], 24000)
            self.assertEqual(validate_srt(srt, 1.0)["cue_count"], 1)
            self.assertEqual(validate_image(image)["width"], 1280)

    def test_rejects_bad_srt_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "bad.srt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("1\n00:00:02,000 --> 00:00:01,000\nbad\n")
            with self.assertRaises(ArtifactValidationError):
                validate_srt(path)

    @patch("src.artifact_validation.subprocess.run")
    def test_video_requires_audio_and_video_streams_and_matching_duration(self, run):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            video_path = handle.name
        try:
            run.return_value.returncode = 0
            run.return_value.stderr = ""
            run.return_value.stdout = json.dumps({
                "streams": [{"codec_type": "video", "codec_name": "h264"},
                            {"codec_type": "audio", "codec_name": "aac"}],
                "format": {"duration": "10.0"},
            })
            self.assertEqual(validate_video(video_path, 10.0)["duration_seconds"], 10.0)
            with self.assertRaises(ArtifactValidationError):
                validate_video(video_path, 20.0)
        finally:
            os.remove(video_path)


if __name__ == "__main__":
    unittest.main()
