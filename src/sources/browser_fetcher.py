"""Browser-based fetcher for Cloudflare-protected novel sources (e.g. 69shuba)."""
from __future__ import annotations

import atexit
import logging
import time

_driver = None


class BrowserResponse:
    def __init__(self, content: bytes, url: str, status_code: int = 200):
        self.content = content
        self.url = url
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            from .base import SourceAccessError
            raise SourceAccessError(f"HTTP {self.status_code}: {self.url}")


def get_browser_driver():
    global _driver
    if _driver is None:
        from seleniumbase import Driver
        logging.info("[BrowserFetcher] Initializing SeleniumBase UC Driver...")
        _driver = Driver(uc=True, headless=False)
        atexit.register(close_browser_driver)
    return _driver


def close_browser_driver():
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception as e:
            logging.debug(f"[BrowserFetcher] Error closing driver: {e}")
        _driver = None


def fetch_page_browser(url: str, source=None, timeout: int = 25) -> BrowserResponse:
    driver = get_browser_driver()

    if not getattr(driver, "_cf_cleared", False):
        logging.info(f"[BrowserFetcher] First page challenge negotiation on {url}...")
        driver.uc_open_with_reconnect(url, reconnect_time=6)
        for wait_round in range(10):
            page_title = driver.title
            if "Just a moment..." not in page_title and "Cloudflare" not in page_title:
                break
            logging.info(f"[BrowserFetcher] Cloudflare challenge detected ({page_title}), waiting ({wait_round+1}/10)...")
            try:
                driver.uc_gui_click_captcha()
            except Exception:
                pass
            time.sleep(2)
        driver._cf_cleared = True
    else:
        driver.get(url)
        page_title = driver.title
        if "Just a moment..." in page_title or "Cloudflare" in page_title:
            logging.info(f"[BrowserFetcher] Challenge re-detected on {url}, solving...")
            for wait_round in range(5):
                try:
                    driver.uc_gui_click_captcha()
                except Exception:
                    pass
                time.sleep(2)
                if "Just a moment..." not in driver.title and "Cloudflare" not in driver.title:
                    break

    html = driver.page_source
    current_url = driver.current_url or url
    encoding = getattr(source, "encoding", "utf-8") or "utf-8"
    return BrowserResponse(html.encode(encoding, errors="replace"), current_url, status_code=200)
