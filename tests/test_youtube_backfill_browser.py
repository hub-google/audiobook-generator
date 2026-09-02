from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tools.chrome_cookie_harvester import (
    BrowserCardWorker,
    save_cookie_to_env,
)
from tools.youtube_backfill_gui import (
    StateStore,
    StudioPrivateClient,
)


def test_normalize_and_parse_cookie():
    raw = '  "cookie: SAPISID=secret123; SID=sid456; __Secure-3PAPISID=sec3"  '
    normalized = StudioPrivateClient._normalize_cookie(raw)
    assert "cookie:" not in normalized.lower()
    parsed = StudioPrivateClient._parse_cookie(normalized)
    assert parsed["SAPISID"] == "secret123"
    assert parsed["SID"] == "sid456"
    assert parsed["__Secure-3PAPISID"] == "sec3"


def test_authorization_header_generation():
    client = object.__new__(StudioPrivateClient)
    client.cookies = {
        "SAPISID": "test_sapisid_token",
        "__Secure-1PAPISID": "test_1papisid_token",
        "__Secure-3PAPISID": "test_3papisid_token",
    }
    client.config = {"user_session_id": ""}
    
    with patch("tools.youtube_backfill_gui.time.time", return_value=1700000000):
        auth_header = client._authorization("https://studio.youtube.com")
        assert "SAPISIDHASH 1700000000_" in auth_header
        assert "SAPISID1PHASH 1700000000_" in auth_header
        assert "SAPISID3PHASH 1700000000_" in auth_header


def test_build_pin_action_token_structure():
    token = StudioPrivateClient._build_pin_action_token("comment_123", "video_456", "channel_789")
    decoded = base64.urlsafe_b64decode(token + "==")
    assert b"comment_123" in decoded
    assert b"video_456" in decoded
    assert b"channel_789" in decoded
    assert b"comments-section" in decoded


def test_create_comment_params():
    params = StudioPrivateClient._create_comment_params("video_123")
    decoded = base64.b64decode(params)
    assert b"video_123" in decoded


def test_save_cookie_to_env():
    with tempfile.TemporaryDirectory() as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text('EXISTING_VAR="value"\n', encoding="utf-8")
        
        save_cookie_to_env("SAPISID=abc; SID=def", env_path=env_file)
        
        content = env_file.read_text(encoding="utf-8")
        assert 'YOUTUBE_STUDIO_COOKIES="SAPISID=abc; SID=def"' in content
        assert 'EXISTING_VAR="value"' in content
        assert os.environ.get("YOUTUBE_STUDIO_COOKIES") == "SAPISID=abc; SID=def"


def test_state_store_load_and_mark():
    with tempfile.TemporaryDirectory() as temp_dir:
        state_file = Path(temp_dir) / "state.json"
        store = StateStore(path=state_file)
        
        store.mark("video_1", "has_card", True)
        assert store.video("video_1")["has_card"] is True
        assert state_file.exists()
        
        restored = StateStore(path=state_file)
        assert restored.video("video_1")["has_card"] is True


def test_browser_card_worker_auth_detection():
    worker = BrowserCardWorker()
    assert worker._has_studio_auth() is False
    
    mock_context = Mock()
    mock_page = Mock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://studio.youtube.com/channel/UCIUtGUZ24fMsfzZtydQTsPg"
    mock_context.pages = [mock_page]
    mock_context.cookies.return_value = [
        {"name": "SAPISID", "value": "sapisid_val"},
        {"name": "SID", "value": "sid_val"},
    ]
    worker._context = mock_context
    assert worker._has_studio_auth() is True
