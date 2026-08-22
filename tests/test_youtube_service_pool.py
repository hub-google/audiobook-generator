import unittest
import os
import tempfile
import json
from unittest.mock import patch, MagicMock
from src.youtube_api_uploader import YouTubeServicePool

class YouTubeServicePoolTests(unittest.TestCase):
    def test_pool_rotation_on_quota(self):
        pool = YouTubeServicePool()
        # Mock 2 accounts in the pool
        pool.accounts = [
            {"slot": 1, "cs_path": None, "tok_path": None, "client_id": "c1", "client_secret": "s1", "refresh_token": "r1", "service": MagicMock(), "creds": MagicMock(), "exhausted": False},
            {"slot": 2, "cs_path": None, "tok_path": None, "client_id": "c2", "client_secret": "s2", "refresh_token": "r2", "service": MagicMock(), "creds": MagicMock(), "exhausted": False},
        ]
        pool.active_index = 0
        
        # Current active account is Slot 1
        self.assertEqual(pool.active_account["slot"], 1)
        
        # Trigger quota rotation
        rotated = pool.rotate_on_quota(Exception("403 quotaExceeded"))
        self.assertTrue(rotated)
        self.assertEqual(pool.active_account["slot"], 2)
        self.assertTrue(pool.accounts[0]["exhausted"])
        self.assertFalse(pool.accounts[1]["exhausted"])
        
        # Trigger rotation again when all exhausted
        rotated2 = pool.rotate_on_quota(Exception("403 quotaExceeded"))
        self.assertFalse(rotated2)
        self.assertTrue(pool.accounts[1]["exhausted"])

    def test_get_playlist_video_index_rotates_and_recovers(self):
        from src.youtube_api_uploader import get_playlist_video_index
        pool = YouTubeServicePool()
        s1 = MagicMock()
        s1.playlistItems.return_value.list.return_value.execute.side_effect = Exception("403 quotaExceeded")
        s2 = MagicMock()
        s2.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "Part 1", "resourceId": {"videoId": "v1"}}}]
        }
        pool.accounts = [
            {"slot": 1, "cs_path": None, "tok_path": None, "client_id": "c1", "client_secret": "s1", "refresh_token": "r1", "service": s1, "creds": MagicMock(), "exhausted": False},
            {"slot": 2, "cs_path": None, "tok_path": None, "client_id": "c2", "client_secret": "s2", "refresh_token": "r2", "service": s2, "creds": MagicMock(), "exhausted": False},
        ]
        pool.active_index = 0
        index = get_playlist_video_index(pool, "playlist-1")
        self.assertEqual(index, {"Part 1": "v1"})
        self.assertEqual(pool.active_account["slot"], 2)

    def test_get_playlist_video_index_pauses_when_all_exhausted(self):
        from src.youtube_api_uploader import get_playlist_video_index, UploadPaused
        pool = YouTubeServicePool()
        s1 = MagicMock()
        s1.playlistItems.return_value.list.return_value.execute.side_effect = Exception("403 quotaExceeded")
        pool.accounts = [
            {"slot": 1, "cs_path": None, "tok_path": None, "client_id": "c1", "client_secret": "s1", "refresh_token": "r1", "service": s1, "creds": MagicMock(), "exhausted": False},
        ]
        pool.active_index = 0
        with self.assertRaises(UploadPaused) as raised:
            get_playlist_video_index(pool, "playlist-1")
        self.assertEqual(raised.exception.reason, "quotaExceeded")

    def test_add_video_to_playlist_rotates_and_recovers(self):
        from src.youtube_api_uploader import add_video_to_playlist
        pool = YouTubeServicePool()
        s1 = MagicMock()
        s1.playlistItems.return_value.insert.return_value.execute.side_effect = Exception("403 quotaExceeded")
        s2 = MagicMock()
        s2.playlistItems.return_value.insert.return_value.execute.return_value = {"id": "item-1"}
        pool.accounts = [
            {"slot": 1, "cs_path": None, "tok_path": None, "client_id": "c1", "client_secret": "s1", "refresh_token": "r1", "service": s1, "creds": MagicMock(), "exhausted": False},
            {"slot": 2, "cs_path": None, "tok_path": None, "client_id": "c2", "client_secret": "s2", "refresh_token": "r2", "service": s2, "creds": MagicMock(), "exhausted": False},
        ]
        pool.active_index = 0
        success = add_video_to_playlist(pool, "playlist-1", "video-1", position=0)
        self.assertTrue(success)
        self.assertEqual(pool.active_account["slot"], 2)

    def test_add_video_to_playlist_pauses_when_all_exhausted(self):
        from src.youtube_api_uploader import add_video_to_playlist, UploadPaused
        pool = YouTubeServicePool()
        s1 = MagicMock()
        s1.playlistItems.return_value.insert.return_value.execute.side_effect = Exception("403 quotaExceeded")
        pool.accounts = [
            {"slot": 1, "cs_path": None, "tok_path": None, "client_id": "c1", "client_secret": "s1", "refresh_token": "r1", "service": s1, "creds": MagicMock(), "exhausted": False},
        ]
        pool.active_index = 0
        with self.assertRaises(UploadPaused) as raised:
            add_video_to_playlist(pool, "playlist-1", "video-1", position=0)
        self.assertEqual(raised.exception.reason, "quotaExceeded")

if __name__ == "__main__":
    unittest.main()
