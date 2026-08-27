import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.huggingface_archiver import HuggingFaceArchiver


class HuggingFaceArchiverStateTests(unittest.TestCase):
    def make_archiver(self, directory):
        archiver = HuggingFaceArchiver.__new__(HuggingFaceArchiver)
        archiver.repo_id = "owner/archive"
        archiver.project = "有聲小說"
        archiver.api = Mock()
        archiver.state_file = Path(directory) / "archive_state.json"
        archiver.state = {"schema_version": 1, "repo_id": archiver.repo_id, "books": {}}
        return archiver

    def test_completed_parts_rejects_legacy_complete_record_without_youtube_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            archiver = self.make_archiver(directory)
            archiver.state["books"]["書"] = {
                "parts": {"1": {"status": "complete"}},
                "root": "有聲小說/書",
            }

            self.assertEqual(set(), archiver.completed_parts("書"))

    def test_register_preuploaded_part_stays_pending_until_youtube_is_finalized(self):
        with tempfile.TemporaryDirectory() as directory:
            archiver = self.make_archiver(directory)
            video = Path(directory) / "part.mp4"
            subtitle = Path(directory) / "part.srt"
            cover = Path(directory) / "cover.jpg"
            video.write_bytes(b"video")
            subtitle.write_bytes(b"subtitle")
            cover.write_bytes(b"cover")
            part_root = archiver._part_root("書", 1, 1, 10)
            archiver.api.list_repo_files.return_value = [
                f"{part_root}/part.mp4",
                f"{part_root}/part.srt",
                f"{part_root}/merge_manifest.json",
                f"{part_root}/part_manifest.json",
                f"{part_root}/media_info.json",
            ]

            record = archiver.register_preuploaded_part(
                book_title="書", part_num=1, start_chap=1, end_chap=10,
                chapters=range(1, 11), video_path=video, subtitle_path=subtitle,
                master_cover_path=cover,
            )

            self.assertEqual("uploaded_pending_youtube_metadata", record["status"])
            self.assertEqual(
                "uploaded_pending_youtube_metadata", record["manifest"]["status"]
            )
            self.assertEqual(set(), archiver.completed_parts("書"))

    def test_finalize_part_is_the_transition_to_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            archiver = self.make_archiver(directory)
            archiver.state["books"]["書"] = {
                "parts": {
                    "1": {
                        "status": "uploaded_pending_youtube_metadata",
                        "root": "有聲小說/書/part-1",
                        "manifest": {"status": "uploaded_pending_youtube_metadata"},
                    }
                },
                "root": "有聲小說/書",
            }

            archiver.finalize_part(
                book_title="書", part_num=1, youtube_video_id="video-1",
                playlist_id="playlist-1", title="Part 1", description="description",
                privacy="public", playlist_position=0,
            )

            self.assertEqual({1}, archiver.completed_parts("書"))
            record = archiver.state["books"]["書"]["parts"]["1"]
            self.assertEqual("complete", record["status"])
            self.assertEqual("video-1", record["youtube"]["youtube_video_id"])

    def test_verify_book_reports_part_missing_youtube_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            archiver = self.make_archiver(directory)
            archiver.api.list_repo_files.return_value = []
            archiver.state["books"]["書"] = {
                "parts": {"1": {"status": "complete"}},
                "root": "有聲小說/書",
            }

            with self.assertRaisesRegex(
                RuntimeError, r"Parts missing valid YouTube metadata=\[1\]"
            ):
                archiver.verify_book("書", 1)


if __name__ == "__main__":
    unittest.main()
