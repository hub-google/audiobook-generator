"""Browser smoke test and screenshots for the actual GitHub Pages web console."""
import contextlib
import base64
import json
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
            # Mock GitHub only inside the local browser so task editing and
            # review can be exercised without a real PAT or modifying production.
            state = {
                "schema_version": 3, "revision": 1,
                "updated_at": "2026-11-27T00:00:00Z", "completed": [],
                "queue": [
                    {"task_id": "book-smoke-queued", "book_title": "原創測試小說",
                     "catalog_url": "https://czbooks.net/n/demo", "start_chapter": 1,
                     "end_chapter": 3, "excluded_chapters": [],
                     "chapter_title_overrides": {}, "chapter_order": [],
                     "status": "paused", "stages": {}},
                    {"task_id": "book-smoke-review", "book_title": "封面審核測試",
                     "catalog_url": "https://czbooks.net/n/demo-review",
                     "start_chapter": 1, "end_chapter": 2,
                     "status": "waiting_review", "scrape_run_id": 123456,
                     "cover_run_id": 123457,
                     "stages": {"scrape": {"status": "completed"},
                                "cover": {"status": "completed"}},
                     "ad_review": {"status": "pending"}},
                    {"task_id": "book-smoke-running", "book_title": "執行中測試",
                     "catalog_url": "https://czbooks.net/n/demo-running", "start_chapter": 1,
                     "end_chapter": 2, "run_id": 24680,
                     "status": "running", "stages": {"processing": {"status": "running"}}},
                ]
            }
            mock_writes = []
            def github_mock(route):
                request = route.request
                if request.url.endswith("/user"):
                    result = {"login": "audio-ui-smoke"}
                    status = 200
                elif "/contents/audiobook-queue.json" in request.url:
                    if request.method == "PUT":
                        payload = request.post_data_json
                        updated = json.loads(base64.b64decode(payload["content"]))
                        state.clear()
                        state.update(updated)
                        mock_writes.append(updated)
                        result = {"content": {"sha": "mock-sha"}}
                    else:
                        result = {"content": base64.b64encode(
                            json.dumps(state, ensure_ascii=False).encode()).decode(),
                            "sha": "mock-sha"}
                    status = 200
                elif request.url.endswith("/actions/runs?per_page=35"):
                    result = {"workflow_runs": []}
                    status = 200
                elif request.url.endswith("/actions/runs/24680"):
                    result = {"id": 24680, "status": "in_progress"}
                    status = 200
                elif request.url.endswith("/actions/runs/24680/cancel"):
                    route.fulfill(status=202, body="")
                    return
                elif "/actions/workflows/" in request.url and request.method == "POST":
                    route.fulfill(status=204, body="")
                    return
                else:
                    result = {"message": "Not available in smoke environment"}
                    status = 404
                route.fulfill(status=status, content_type="application/json",
                              body=json.dumps(result, ensure_ascii=False))
            desktop.route("https://api.github.com/**", github_mock)
            desktop.on("dialog", lambda dialog: dialog.accept())
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
            desktop.locator('[data-tab="overview"]').click()
            desktop.locator("#token").fill("ghp_mock_local_browser_only")
            desktop.locator("#connect-btn").click()
            desktop.locator("#connection-pill").filter(has_text="已連線").wait_for()
            desktop.locator("#queue-body tr").first.get_by_role("button", name="編輯").click()
            assert desktop.locator("#edit-dialog").is_visible()
            desktop.locator("#edit-name").fill("原創測試新版")
            desktop.locator("#edit-titles").fill("1 | 第一章 原創內容")
            desktop.screenshot(path=str(OUTPUT / "audio-task-editor.png"), full_page=True)
            desktop.locator("#edit-form button[type=submit]").click()
            desktop.locator("#edit-dialog").wait_for(state="hidden")
            assert mock_writes[-1]["queue"][0]["book_title"] == "原創測試新版"
            assert mock_writes[-1]["queue"][0]["chapter_title_overrides"]["1"] == "第一章 原創內容"
            desktop.locator("#queue-body tr").nth(1).get_by_role(
                "button", name="確認審核並繼續").click()
            assert desktop.locator("#review-dialog").is_visible()
            desktop.locator("#review-patterns").fill("本段為測試廣告")
            desktop.screenshot(path=str(OUTPUT / "audio-review-editor.png"), full_page=True)
            desktop.locator("#review-form button[type=submit]").click()
            desktop.locator("#review-dialog").wait_for(state="hidden")
            assert mock_writes[-1]["queue"][1]["ad_review"]["remove_patterns"] == ["本段為測試廣告"]
            desktop.locator("#queue-body tr").first.get_by_role("button", name="進度").click()
            assert desktop.locator("#progress-dialog").is_visible()
            assert "原創測試新版" in desktop.locator("#progress-detail").inner_text()
            desktop.locator("#progress-close").click()
            desktop.locator("#queue-body tr").nth(2).get_by_role(
                "button", name="取消本次 Run").click()
            desktop.get_by_text("已提交取消請求").wait_for()
            assert next(t for t in mock_writes[-1]["queue"]
                        if t["task_id"] == "book-smoke-running")["status"] == "canceling"
            desktop.locator("#queue-body tr").first.get_by_role("button", name="標為已完成").click()
            assert len(mock_writes[-1]["completed"]) == 1
            assert mock_writes[-1]["completed"][0]["task_id"] == "book-smoke-queued"
            desktop.locator("#completed-list .completed-item").first.get_by_role(
                "button", name="移回佇列").click()
            assert len(mock_writes[-1]["completed"]) == 0
            assert next(t for t in mock_writes[-1]["queue"]
                        if t["task_id"] == "book-smoke-queued")["status"] == "paused"
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
