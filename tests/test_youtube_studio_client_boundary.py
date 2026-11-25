"""Studio account backend can be imported without a Tk root."""
from tools import youtube_backfill_backend as backend
from tools import youtube_studio_client
from tools import youtube_backfill_gui


def test_account_client_is_a_single_network_class():
    assert backend.StudioPrivateClient is youtube_studio_client.StudioPrivateClient
    assert youtube_backfill_gui.StudioPrivateClient is youtube_studio_client.StudioPrivateClient


def test_studio_client_is_not_declared_inside_state_or_tk_module():
    from pathlib import Path
    code = Path(backend.__file__).read_text(encoding="utf-8")
    assert "class StudioPrivateClient:" not in code
    gui = Path(youtube_backfill_gui.__file__).read_text(encoding="utf-8")
    assert "class StudioPrivateClient:" not in gui
