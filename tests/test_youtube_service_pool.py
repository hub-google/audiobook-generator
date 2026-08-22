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

if __name__ == "__main__":
    unittest.main()
