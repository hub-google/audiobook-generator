import unittest
import os
import tempfile
import json
from unittest.mock import patch, MagicMock
from src.youtube_api_uploader import YouTubeServicePool, configured_youtube_account_slots

class YouTubeServicePoolTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "YOUTUBE_CLIENT_ID": "c1", "YOUTUBE_CLIENT_SECRET": "s1", "YOUTUBE_REFRESH_TOKEN": "r1",
        "YOUTUBE_CLIENT_ID_2": "c2", "YOUTUBE_CLIENT_SECRET_2": "s2", "YOUTUBE_REFRESH_TOKEN_2": "r2",
        "YOUTUBE_CLIENT_ID_3": "incomplete",
    }, clear=True)
    def test_configured_slots_count_only_complete_credential_triplets(self):
        self.assertEqual(configured_youtube_account_slots(), {1, 2})

    @patch.dict(os.environ, {
        "YOUTUBE_CLIENT_ID_1": "c1", "YOUTUBE_CLIENT_SECRET": "s1",
        "YOUTUBE_REFRESH_TOKEN_1": "r1",
    }, clear=True)
    def test_slot_one_allows_mixed_legacy_and_numbered_names(self):
        self.assertEqual(configured_youtube_account_slots(), {1})

    @patch.dict(os.environ, {
        "YOUTUBE_EXPECTED_ACCOUNT_COUNT": "2",
        "YOUTUBE_CLIENT_ID": "c1", "YOUTUBE_CLIENT_SECRET": "s1", "YOUTUBE_REFRESH_TOKEN": "r1",
        "YOUTUBE_CLIENT_ID_2": "c2", "YOUTUBE_CLIENT_SECRET_2": "s2",
    }, clear=True)
    def test_expected_pool_rejects_partially_configured_slot(self):
        pool = YouTubeServicePool()
        with self.assertRaisesRegex(RuntimeError, "found 1/2 accounts; missing slots: 2"):
            pool.require_expected_accounts()

    @patch.dict(os.environ, {
        "YOUTUBE_EXPECTED_ACCOUNT_COUNT": "5",
        "YOUTUBE_CLIENT_ID": "c1",
        "YOUTUBE_CLIENT_SECRET": "s1",
        "YOUTUBE_REFRESH_TOKEN": "r1",
    }, clear=True)
    def test_expected_pool_size_rejects_silent_single_account_fallback(self):
        with patch("src.youtube_api_uploader.os.path.exists", return_value=False):
            pool = YouTubeServicePool()
        with self.assertRaisesRegex(RuntimeError, "found 1/5 accounts"):
            pool.require_expected_accounts()

    @patch.dict(os.environ, {
        "YOUTUBE_EXPECTED_ACCOUNT_COUNT": "5",
        **{
            f"YOUTUBE_{field}_{slot}": f"{field.lower()}-{slot}"
            for slot in range(1, 6)
            for field in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN")
        },
    }, clear=True)
    def test_discovers_all_five_numbered_accounts(self):
        with patch("src.youtube_api_uploader.os.path.exists", return_value=False):
            pool = YouTubeServicePool()
        self.assertEqual([account["slot"] for account in pool.accounts], [1, 2, 3, 4, 5])
        pool.require_expected_accounts()

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

    def test_pool_accepts_projects_authorized_for_same_channel(self):
        pool = YouTubeServicePool()
        services = [MagicMock(), MagicMock()]
        for service in services:
            service.channels.return_value.list.return_value.execute.return_value = {
                "items": [{"id": "channel-1"}]
            }
        pool.accounts = [
            {"slot": slot, "service": service, "creds": MagicMock(), "channel_id": None,
             "exhausted": False}
            for slot, service in enumerate(services, 1)
        ]

        self.assertEqual(pool.require_same_channel(), "channel-1")
        self.assertEqual([account["channel_id"] for account in pool.accounts], ["channel-1", "channel-1"])

    def test_pool_rejects_projects_authorized_for_different_channels(self):
        pool = YouTubeServicePool()
        services = [MagicMock(), MagicMock()]
        for service, channel_id in zip(services, ["channel-1", "channel-2"]):
            service.channels.return_value.list.return_value.execute.return_value = {
                "items": [{"id": channel_id}]
            }
        pool.accounts = [
            {"slot": slot, "service": service, "creds": MagicMock(), "channel_id": None,
             "exhausted": False}
            for slot, service in enumerate(services, 1)
        ]

        with self.assertRaisesRegex(RuntimeError, "spans different channels"):
            pool.require_same_channel()

    def test_pool_channel_validation_preserves_quota_pause(self):
        from src.youtube_api_uploader import UploadPaused
        pool = YouTubeServicePool()
        service = MagicMock()
        service.channels.return_value.list.return_value.execute.side_effect = Exception("quotaExceeded")
        pool.accounts = [
            {"slot": 1, "service": service, "creds": MagicMock(), "channel_id": None,
             "exhausted": False},
            {"slot": 2, "service": MagicMock(), "creds": MagicMock(), "channel_id": None,
             "exhausted": False},
        ]

        with self.assertRaises(UploadPaused):
            pool.require_same_channel()

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
