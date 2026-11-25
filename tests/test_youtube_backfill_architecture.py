"""YouTube Studio backfill separates network requests from Tk widget lifecycle."""
from tools import youtube_backfill_backend, youtube_backfill_gui
from tkinter import ttk


def test_youtube_backfill_backend_and_gui_share_one_client_and_state_store():
    assert youtube_backfill_gui.StudioPrivateClient is youtube_backfill_backend.StudioPrivateClient
    assert youtube_backfill_gui.StateStore is youtube_backfill_backend.StateStore
    assert youtube_backfill_gui.VideoRow is youtube_backfill_backend.VideoRow
    assert issubclass(youtube_backfill_gui.App, ttk.Frame)


def test_gui_has_no_second_studio_client_implementation():
    from pathlib import Path
    gui = Path(youtube_backfill_gui.__file__).read_text(encoding="utf-8")
    assert "class StudioPrivateClient" not in gui
    assert "class StateStore" not in gui
    assert "def _authorization(" not in gui
