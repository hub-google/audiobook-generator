import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.prepare_parts import plan_parts


class PreparePartsTests(unittest.TestCase):
    def test_locks_all_parts_and_builds_bounded_merge_matrix(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = root / "config.yaml"
            config.write_text(yaml.safe_dump({
                "book_title": "測試書",
                "selected_indices": [1, 2],
            }, allow_unicode=True), encoding="utf-8")

            def download(_run, _repo, name, destination):
                destination = Path(destination)
                (destination / "Video").mkdir(parents=True)
                (destination / "Subtitles").mkdir(parents=True)
                number = int(name.rsplit("-", 1)[1]) + 1
                (destination / "Video" / f"測試書_chapter_{number}.mp4").write_bytes(b"video")
                (destination / "Subtitles" / f"測試書_chapter_{number}.srt").write_text("srt", encoding="utf-8")
                return True

            def scan(directory, artifact):
                number = int(artifact.rsplit("-", 1)[1]) + 1
                base = Path(directory)
                return [{
                    "artifact": artifact,
                    "chap_num": number,
                    "dur": 20_000.0,
                    "path": str(base / "Video" / f"測試書_chapter_{number}.mp4"),
                    "srt_path": str(base / "Subtitles" / f"測試書_chapter_{number}.srt"),
                }]

            with patch("src.prepare_parts.get_run_artifact_names", return_value=["mp4-worker-0", "mp4-worker-1"]), \
                 patch("src.prepare_parts.download_artifact_task", side_effect=download), \
                 patch("src.prepare_parts.scan_artifact_chapters", side_effect=scan), \
                 patch("src.prepare_parts.confirmed_missing_from_directory", return_value=set()), \
                 patch("src.prepare_parts.validate_chapter_inventory"):
                manifest = plan_parts(
                    "123", "owner/repo", config, root / "out", root / "work"
                )

            self.assertEqual([part["part_num"] for part in manifest["parts"]], [1, 2])
            saved = json.loads((root / "out" / "parts-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["source_run_id"], "123")
            self.assertEqual(saved["chapter_artifacts"], {"1": "mp4-worker-0", "2": "mp4-worker-1"})
            self.assertEqual(len(saved["matrix"]["include"]), 2)
            self.assertLessEqual(len(saved["matrix"]["include"]), 17)
            self.assertTrue((root / "out" / "config.yaml").is_file())


if __name__ == "__main__":
    unittest.main()
