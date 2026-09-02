from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "youtube_backfill_gui.py"
SPEC = importlib.util.spec_from_file_location("youtube_backfill_gui", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeDriver:
    def __init__(self, studio_url: str = "https://studio.youtube.com/") -> None:
        self.current_url = ""
        self.studio_url = studio_url
        self.visits: list[str] = []
        self.cookies: list[dict[str, object]] = []

    def get(self, url: str) -> None:
        self.visits.append(url)
        self.current_url = self.studio_url if "studio.youtube.com" in url else url

    def add_cookie(self, cookie: dict[str, object]) -> None:
        assert "youtube.com" in self.current_url
        self.cookies.append(cookie)

    def execute_script(self, _script: str) -> str:
        return "test-api-key"

    def quit(self) -> None:
        pass


class FakeWait:
    def __init__(self, driver: FakeDriver, _timeout: int) -> None:
        self.driver = driver

    def until(self, condition):
        if not condition(self.driver):
            raise TimeoutError
        return True


def make_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, driver: FakeDriver):
    monkeypatch.setattr(MODULE, "CHROME_BINARY", MODULE_PATH)
    monkeypatch.setattr(MODULE, "CHROME_PROFILE", tmp_path / "profile")
    monkeypatch.setattr(MODULE.webdriver, "Chrome", lambda options: driver)
    monkeypatch.setattr(MODULE, "WebDriverWait", FakeWait)
    return MODULE.StudioCardBrowser("SAPISID=abc; SID=def", Mock())


def test_cookie_is_injected_on_youtube_before_opening_studio(monkeypatch, tmp_path):
    driver = FakeDriver()
    make_browser(monkeypatch, tmp_path, driver)

    assert driver.visits == ["https://www.youtube.com/", "https://studio.youtube.com/"]
    assert {cookie["name"] for cookie in driver.cookies} == {"SAPISID", "SID"}
    assert all(cookie["domain"] == ".youtube.com" for cookie in driver.cookies)


def test_login_redirect_stops_batch_instead_of_sending_api(monkeypatch, tmp_path):
    driver = FakeDriver("https://accounts.google.com/ServiceLogin")

    with pytest.raises(RuntimeError, match="尚未登入"):
        make_browser(monkeypatch, tmp_path, driver)


def test_edit_is_blocked_outside_studio():
    browser = object.__new__(MODULE.StudioCardBrowser)
    browser.driver = Mock(current_url="https://accounts.google.com/ServiceLogin")

    ok, message = browser._execute_in_page_edit({})

    assert not ok
    assert "已停止送出" in message
    browser.driver.execute_async_script.assert_not_called()


def test_edit_sends_signed_studio_authorization_headers():
    browser = object.__new__(MODULE.StudioCardBrowser)
    browser.driver = Mock(current_url="https://studio.youtube.com/")
    browser.driver.execute_async_script.return_value = {
        "status": 200,
        "ok": True,
        "json": {},
    }
    browser.verifier = Mock()
    browser.verifier._headers.return_value = {
        "Authorization": "SAPISIDHASH signed-value",
        "X-Origin": "https://studio.youtube.com",
        "X-Goog-AuthUser": "0",
        "Cookie": "SAPISID=secret",
        "Origin": "https://studio.youtube.com",
        "Referer": "https://studio.youtube.com/",
        "User-Agent": "test",
        "Content-Type": "application/json",
    }

    ok, message = browser._execute_in_page_edit({"externalVideoId": "abc"})

    assert ok
    assert message == "OK"
    args = browser.driver.execute_async_script.call_args.args
    assert args[2]["Authorization"] == "SAPISIDHASH signed-value"
    assert args[2]["X-Origin"] == "https://studio.youtube.com"
    assert args[2]["X-Goog-AuthUser"] == "0"
    assert "Cookie" not in args[2]
    assert "Origin" not in args[2]
