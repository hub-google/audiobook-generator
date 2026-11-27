"""Browser smoke test and screenshots for the actual GitHub Pages web console."""
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs/site"
OUTPUT = ROOT / "ui-test-screenshots"


def main():
    browser_bin = next((path for name in ("google-chrome", "chromium", "chromium-browser")
                        if (path := shutil.which(name))), None)
    if not browser_bin:
        raise RuntimeError("Chrome/Chromium not available for UI smoke test")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(SITE)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.5)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=browser_bin,
                                                 args=["--no-sandbox"])
            errors = []
            desktop = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            desktop.on("pageerror", lambda error: errors.append(str(error)))
            desktop.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=20000)
            assert desktop.title() == "AUDIO · 有聲小說製作平台"
            assert desktop.locator("#overview").is_visible()
            assert desktop.locator("#connection-pill").count() == 1
            desktop.screenshot(path=str(OUTPUT / "audio-dashboard-desktop.png"), full_page=True)
            desktop.locator('[data-tab="create"]').click()
            assert desktop.locator("#create").is_visible()
            assert desktop.locator("#source option").count() == 9
            desktop.locator("#catalog-url").fill("https://czbooks.net/n/demo")
            desktop.locator("#book-title").fill("驗收示範")
            desktop.locator("#start-chapter").fill("1")
            desktop.locator("#end-chapter").fill("10")
            desktop.screenshot(path=str(OUTPUT / "audio-create-desktop.png"), full_page=True)
            desktop.locator('[data-tab="runs"]').click()
            assert desktop.locator("#runs").is_visible()
            mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1,
                                      is_mobile=True, has_touch=True)
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=20000)
            mobile.screenshot(path=str(OUTPUT / "audio-dashboard-mobile.png"), full_page=True)
            assert mobile.locator('[data-tab="create"]').is_visible()
            mobile.locator('[data-tab="create"]').click()
            assert mobile.locator("#create").is_visible()
            if errors:
                raise AssertionError("Browser JavaScript errors: " + "; ".join(errors[:3]))
            browser.close()
        print("UI smoke test passed: tabs, nine sites, form fields, desktop and mobile screenshots")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
