import os
import tempfile
import unittest
import wave

from src.artifact_validation import validate_srt
from src.subtitle_gen import generate_chapter_srt


class SubtitleGenerationTests(unittest.TestCase):
    @staticmethod
    def _write_wav(path, seconds=1):
        with wave.open(path, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 8000 * seconds)

    def test_punctuation_only_chunk_does_not_create_invalid_cue(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_paths = []
            for index in range(3):
                path = os.path.join(directory, f"part-{index}.wav")
                self._write_wav(path)
                wav_paths.append(path)

            output = os.path.join(directory, "chapter.srt")
            generate_chapter_srt(wav_paths, ["第一句話", "!!!", "第二句話"], output)

            result = validate_srt(output, audio_duration=3.0)
            self.assertEqual(result["cue_count"], 2)
            with open(output, encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("2\n00:00:02,000 --> 00:00:03,000", content)


if __name__ == "__main__":
    unittest.main()
