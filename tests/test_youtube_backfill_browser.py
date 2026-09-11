from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import pytest
except ImportError:
    pytest = None

from tools.chrome_cookie_harvester import (
    BrowserCardWorker,
    save_cookie_to_env,
)
from tools.youtube_backfill_gui import (
    StateStore,
    StudioPrivateClient,
    VideoRow,
    build_navigation_comment_text,
    extract_part_number,
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


def test_extract_part_number():
    assert extract_part_number("[已完結]《修真聊天群》第 1501~1600 章【第 18 部】") == 18
    assert extract_part_number("[已完結]《凡人修仙傳》第 1~90 章【第 1 部】") == 1
    assert extract_part_number("【第 19 部】下一部預告") == 19
    assert extract_part_number("小說合集 第20部 完結") == 20
    assert extract_part_number("Novel Title Part 05") == 5
    assert extract_part_number("【第 7 集】有聲書") == 7
    assert extract_part_number("無部數標題", default=99) == 99
    assert extract_part_number("", default=1) == 1


def test_build_navigation_comment_text_middle_part():
    first = VideoRow(video_id="g9Ku5qHqKsk", title="【第 1 部】", position=0)
    current = VideoRow(video_id="curr_vid_18", title="【第 18 部】", position=17)
    prev_v = VideoRow(video_id="WAN53AmDn84", title="【第 17 部】", position=16)
    next_v = VideoRow(video_id="xIsqFbnTzME", title="【第 19 部】", position=18)
    playlist_id = "PLYHOe8Vx5qQI"

    text = build_navigation_comment_text(
        current_video_id=current.video_id,
        playlist_id=playlist_id,
        first_video=first,
        next_video=next_v,
        prev_video=prev_v,
        next_part_num=19,
        prev_part_num=17,
    )

    expected = (
        "🎧 【從第1部開始聽】：https://www.youtube.com/watch?v=g9Ku5qHqKsk\n"
        "▶️【下一部 第19部】：https://www.youtube.com/watch?v=xIsqFbnTzME\n"
        "⏪【上一部 第17部】：https://www.youtube.com/watch?v=WAN53AmDn84\n"
        "📚  完整播放清單：https://www.youtube.com/playlist?list=PLYHOe8Vx5qQI"
    )
    assert text == expected


def test_build_navigation_comment_text_first_part():
    first = VideoRow(video_id="g9Ku5qHqKsk", title="【第 1 部】", position=0)
    next_v = VideoRow(video_id="part2_id", title="【第 2 部】", position=1)
    playlist_id = "PLYHOe8Vx5qQI"

    text = build_navigation_comment_text(
        current_video_id=first.video_id,
        playlist_id=playlist_id,
        first_video=first,
        next_video=next_v,
        prev_video=None,
        next_part_num=2,
        prev_part_num=None,
    )

    # 第 1 部自身不應出現「從第1部開始聽」與「上一部」
    expected = (
        "▶️【下一部 第2部】：https://www.youtube.com/watch?v=part2_id\n"
        "📚  完整播放清單：https://www.youtube.com/playlist?list=PLYHOe8Vx5qQI"
    )
    assert text == expected


def test_build_navigation_comment_text_last_part():
    first = VideoRow(video_id="g9Ku5qHqKsk", title="【第 1 部】", position=0)
    current = VideoRow(video_id="part20_id", title="【第 20 部】", position=19)
    prev_v = VideoRow(video_id="part19_id", title="【第 19 部】", position=18)
    playlist_id = "PLYHOe8Vx5qQI"

    text = build_navigation_comment_text(
        current_video_id=current.video_id,
        playlist_id=playlist_id,
        first_video=first,
        next_video=None,
        prev_video=prev_v,
        next_part_num=None,
        prev_part_num=19,
    )

    # 最後一部不應出現「下一部」
    expected = (
        "🎧 【從第1部開始聽】：https://www.youtube.com/watch?v=g9Ku5qHqKsk\n"
        "⏪【上一部 第19部】：https://www.youtube.com/watch?v=part19_id\n"
        "📚  完整播放清單：https://www.youtube.com/playlist?list=PLYHOe8Vx5qQI"
    )
    assert text == expected


def test_post_navigation_comment_custom_text():
    client = object.__new__(StudioPrivateClient)
    client._youtube_post = Mock(return_value={"commentId": "cid_999"})
    client._web_context = Mock(return_value={})
    client._create_comment_params = Mock(return_value="mock_params")

    custom_text = "🎧 【從第1部開始聽】：https://www.youtube.com/watch?v=aaa"
    cid, _ = client.post_navigation_comment(
        video_id="vid_123",
        first_video_id="aaa",
        playlist_id="plist_1",
        comment_text=custom_text,
    )

    assert cid == "cid_999"
    client._youtube_post.assert_called_once()
    payload = client._youtube_post.call_args[0][1]
    assert payload["commentText"] == custom_text

