import os
import tempfile
import unittest
import wave
from unittest.mock import patch

from PIL import Image

from src.video_gen import generate_chapter_video


class VideoGenerationDurationTests(unittest.TestCase):
    @patch("src.video_gen.subprocess.run")
    def test_ffmpeg_output_is_capped_at_exact_wav_duration(self, run):
        with tempfile.TemporaryDirectory() as directory:
            book_title = "測試書"
            workspace = os.path.join(directory, "workspace")
            output_dir = os.path.join(workspace, "Video")
            image_dir = os.path.join(workspace, "Images")
            os.makedirs(output_dir)
            os.makedirs(image_dir)

            wav_path = os.path.join(directory, f"{book_title}_chapter_1.wav")
            with wave.open(wav_path, "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b"\0\0" * 30012)

            Image.new("RGB", (1280, 720), "navy").save(
                os.path.join(image_dir, f"{book_title}_chapter_1.jpg"), "JPEG"
            )

            def create_partial(command, **_kwargs):
                with open(command[-1], "wb") as handle:
                    handle.write(b"0" * 1001)

            run.side_effect = create_partial
            generate_chapter_video(book_title, wav_path, workspace, output_dir, [])

            command = run.call_args.args[0]
            self.assertNotIn("-shortest", command)
            duration_index = command.index("-t")
            self.assertEqual(command[duration_index + 1], "1.250500")
            self.assertEqual(duration_index, len(command) - 3)


if __name__ == "__main__":
    unittest.main()
