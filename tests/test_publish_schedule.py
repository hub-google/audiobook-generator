import unittest
from unittest.mock import Mock, patch
from src.cloud_queue import empty_queue, new_task, update_task_chapters
from src.queue_dispatcher import Dispatcher
from src.youtube_upload.media import upload_video_file


class PublishScheduleTests(unittest.TestCase):
    def test_new_task_with_publish_at(self):
        task = new_task(
            catalog_url="https://example.com/book/1",
            book_title="測試小說",
            start_chapter=1,
            end_chapter=10,
            publish_at="2026-09-11T18:00:00+08:00",
        )
        self.assertEqual(task["publish_at"], "2026-09-11T18:00:00+08:00")

    def test_new_task_default_publish_at_is_none(self):
        task = new_task(
            catalog_url="https://example.com/book/1",
            book_title="測試小說",
            start_chapter=1,
            end_chapter=10,
        )
        self.assertIsNone(task["publish_at"])

    def test_update_task_chapters_updates_publish_at(self):
        queue = empty_queue()
        task = new_task(
            catalog_url="https://example.com/book/1",
            book_title="測試小說",
            start_chapter=1,
            end_chapter=10,
        )
        queue["queue"].append(task)

        updated_queue = update_task_chapters(
            queue,
            task["task_id"],
            start_chapter=1,
            end_chapter=20,
            publish_at="2026-09-12T18:00:00+08:00",
        )
        updated_task = updated_queue["queue"][0]
        self.assertEqual(updated_task["publish_at"], "2026-09-12T18:00:00+08:00")

    def test_dispatcher_passes_publish_at_to_inputs(self):
        dispatcher = Dispatcher("owner/repo", "token")
        dispatcher.request = Mock(return_value=Mock(status_code=204, text=""))
        dispatcher.runs = Mock(return_value=[])
        dispatcher.cover_runs = Mock(return_value=[])

        task = new_task(
            catalog_url="https://example.com/book/1",
            book_title="測試小說",
            start_chapter=1,
            end_chapter=10,
            publish_at="2026-09-11T18:00:00+08:00",
        )
        queue = {"queue": [task], "completed": []}
        with patch.object(dispatcher.store, "load", return_value=(queue, "sha-123")), \
             patch.object(dispatcher.store, "save"), \
             patch.object(dispatcher.profile_store, "load", return_value=({}, "sha-profile")):
            dispatcher.dispatch_next(queue)

        call_args = dispatcher.request.call_args_list
        audiobook_dispatches = [
            c for c in call_args
            if len(c[0]) >= 2 and "/audiobook.yml/dispatches" in c[0][1]
        ]
        self.assertTrue(len(audiobook_dispatches) >= 1)
        sent_inputs = audiobook_dispatches[0][1]["json"]["inputs"]
        self.assertEqual(sent_inputs.get("publish_at"), "2026-09-11T18:00:00+08:00")

    @patch("src.youtube_upload.media.os.path.getsize", return_value=1024 * 1024)
    @patch("src.youtube_upload.media.MediaFileUpload")
    def test_upload_video_file_with_scheduled_publish(self, mock_media_cls, mock_getsize):
        mock_youtube = Mock()
        mock_request = Mock()
        mock_request.next_chunk.return_value = (Mock(progress=lambda: 1.0), {"id": "test_vid_123"})
        mock_youtube.videos().insert.return_value = mock_request

        upload_video_file(
            mock_youtube,
            video_path="dummy.mp4",
            title="測試影片",
            description="測試描述",
            publish_at="2026-09-11T18:00:00+08:00",
        )

        call_kwargs = mock_youtube.videos().insert.call_args[1]
        status = call_kwargs["body"]["status"]
        self.assertEqual(status["privacyStatus"], "private")
        self.assertEqual(status["publishAt"], "2026-09-11T18:00:00+08:00")

    @patch("src.youtube_upload.media.os.path.getsize", return_value=1024 * 1024)
    @patch("src.youtube_upload.media.MediaFileUpload")
    def test_upload_video_file_immediate_public(self, mock_media_cls, mock_getsize):
        mock_youtube = Mock()
        mock_request = Mock()
        mock_request.next_chunk.return_value = (Mock(progress=lambda: 1.0), {"id": "test_vid_123"})
        mock_youtube.videos().insert.return_value = mock_request

        upload_video_file(
            mock_youtube,
            video_path="dummy.mp4",
            title="測試影片",
            description="測試描述",
            publish_at=None,
        )

        call_kwargs = mock_youtube.videos().insert.call_args[1]
        status = call_kwargs["body"]["status"]
        self.assertEqual(status["privacyStatus"], "public")
        self.assertNotIn("publishAt", status)


if __name__ == "__main__":
    unittest.main()
