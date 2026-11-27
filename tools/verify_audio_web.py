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
            desktop.route("**/full-novels.json", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"novels":[
                    {"rank":1,"title":"完本示例甲","author":"測試甲","category":"玄幻","latest_chapter":"第100章",
                     "catalog_url":"https://69shuba.com/book/111/"},
                    {"rank":2,"title":"完本示例乙","author":"測試乙","category":"都市","latest_chapter":"第200章",
                     "catalog_url":"https://69shuba.com/book/222/"}
                ]}, ensure_ascii=False)))

            desktop.on("dialog", lambda dialog: dialog.accept())
            desktop.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=20000)
            assert desktop.title() == "AUDIO · 有聲小說製作平台"
            assert desktop.locator("#overview").is_visible()
            assert desktop.locator("#connection-pill").count() == 1
            desktop.screenshot(path=str(OUTPUT / "audio-dashboard-desktop.png"), full_page=True)
            desktop.locator("#queue-body tr").first.get_by_role("button", name="進度").wait_for()
            assert desktop.locator("#queue-body tr").first.get_by_role("button", name="進度").is_enabled()
            desktop.locator('#queue-body tr').first.locator('input[type="checkbox"]').check()
            desktop.locator("#batch-cover-btn").click()
            assert desktop.locator("#batch-cover-dialog").is_visible()
            assert desktop.locator("#batch-cover-book-list button").count() == 1
            desktop.locator("#batch-cover-close").click()
            desktop.route("https://raw.githubusercontent.com/**/audiobook-queue.json**",
                lambda route:route.fulfill(
                    status=200,content_type="application/json",
                    headers={"Access-Control-Allow-Origin":"*"},
                    body=json.dumps(state,ensure_ascii=False)))
            desktop.locator("#queue-body tr").first.get_by_role("button", name="進度").click()
            assert desktop.locator("#progress-dialog").is_visible()
            desktop.locator("#progress-refresh-now").click()
            desktop.get_by_text("最近同步：",exact=False).wait_for(timeout=8000)
            assert "原創測試小說" in desktop.locator("#progress-detail").inner_text()
            desktop.locator("#progress-close").click()

            desktop.locator('[data-tab="create"]').click()
            assert desktop.locator("#create").is_visible()
            assert desktop.locator("#source option").count() == 9
            desktop.locator("#rank-body tr").first.get_by_text("完本示例甲").wait_for()
            desktop.locator("#rank-search").fill("完本示例乙")
            assert desktop.locator("#rank-body tr").count() == 1
            desktop.locator("#rank-body tr").first.locator("input[type=checkbox]").check()
            assert desktop.locator("#rank-copy").is_enabled()
            desktop.locator("#rank-search").fill("")
            desktop.locator("#source").select_option("czbooks")
            desktop.locator("#catalog-url").fill("https://czbooks.net/n/demo")
            desktop.locator("#book-title").fill("驗收示範")
            desktop.locator("#start-chapter").fill("1")
            desktop.locator("#end-chapter").fill("10")
            desktop.screenshot(path=str(OUTPUT / "audio-create-desktop.png"), full_page=True)
            desktop.locator('[data-tab="overview"]').click()
            assert desktop.locator("#token").count() == 0
            assert desktop.locator("#connection-pill").inner_text() == "GitHub 登入操作"
            desktop.evaluate("window.__issueUrls=[];window.open=(url)=>{window.__issueUrls.push({url:String(url),stack:(new Error()).stack});return null;};void 0;")
            desktop.locator('[data-tab="create"]').click()
            # Exercise the real catalog response path: desktop defaults to the full book.
            desktop.locator("#start-chapter").fill("2")
            desktop.locator("#end-chapter").fill("3")
            desktop.locator("#excluded").fill("2")
            desktop.locator("details.advanced summary").click()
            desktop.locator("#title-overrides").fill("2 | 舊小說")
            desktop.locator("#number-overrides").fill("2 | 8")
            desktop.locator("#chapter-order").fill("3")
            desktop.locator("#renumber").uncheck()
            catalog_preview = {
                "requested_url": "https://czbooks.net/n/demo",
                "catalog_url": "https://czbooks.net/n/demo",
                "book_title": "全書驗收", "total_chapters": 120,
                "chapter_titles": [f"第{i}章" for i in range(1, 121)],
                "chapter_parts": [{"n": str(i), "d": f"第{i}章", "name": "", "c": ""}
                                  for i in range(1, 121)],
            }
            desktop.route("https://raw.githubusercontent.com/**/web-previews/**", lambda route: route.fulfill(
                status=200, content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=json.dumps(catalog_preview, ensure_ascii=False)))
            desktop.locator("#parse-btn").click()
            desktop.get_by_text("完整目錄解析成功，共 120 章").wait_for(timeout=15000)
            assert desktop.locator("#start-chapter").input_value() == "1"
            assert desktop.locator("#end-chapter").input_value() == "120"
            assert desktop.locator("#renumber").is_checked()
            for field in ("excluded", "title-overrides", "number-overrides", "chapter-order"):
                assert desktop.locator("#"+field).input_value() == ""
            desktop.evaluate("window.__issueUrls=[]")
            desktop.locator("#start-chapter").fill("1")
            desktop.locator("#end-chapter").fill("3")
            desktop.evaluate("""() => {
                parsedCatalog = {
                    catalog_url: "https://czbooks.net/n/demo",
                    book_title: "驗收小說", total_chapters: 3,
                    chapter_titles: ["第一章 開始", "第二章 進行", "第三章 開始"],
                    chapter_parts: [
                        {n:"1",d:"第一章",c:"開始"},
                        {n:"2",d:"第二章",c:"進行"},
                        {n:"3",d:"第三章",c:"開始"}
                    ]
                };
                document.querySelector("#chapter-selection-tools").classList.remove("hidden");
            }""")
            desktop.locator("#open-chapter-picker").click()
            assert desktop.locator("#chapter-dialog").is_visible()
            assert desktop.locator("#chapter-list .chapter-item").count() == 3
            desktop.locator("#duplicate-only").check()
            assert desktop.locator("#chapter-list .chapter-item").count() == 2
            desktop.locator("#duplicate-only").uncheck()
            desktop.locator("#chapter-list .chapter-item").nth(1).locator('input[aria-label="製作 UUID 2"]').uncheck()
            desktop.locator("#chapter-list .chapter-item").first.get_by_role("button",name="編輯").click()
            assert desktop.locator("#chapter-edit-dialog").is_visible()
            desktop.locator("#chapter-edit-name").fill("新的章節名稱")
            desktop.locator("#chapter-edit-number").fill("1.5")
            desktop.locator("#chapter-edit-save").click()
            assert "1 | 1.5" in desktop.locator("#number-overrides").input_value()
            desktop.locator("#chapter-picker-save").click()
            assert desktop.locator("#excluded").input_value() == "2"
            assert desktop.locator("#book-form").evaluate("(el) => el.checkValidity()"), "Create form is incomplete"
            desktop.locator("#book-form button[type=submit]").click()
            desktop.wait_for_function("window.__issueUrls.length >= 1", timeout=7000)
            assert any("/issues/new?" in x["url"] for x in desktop.evaluate("window.__issueUrls")), repr(desktop.evaluate("window.__issueUrls"))
            assert any("AUDIO-COMMAND-V1" in x["url"] for x in desktop.evaluate("window.__issueUrls"))
            desktop.locator('[data-tab="overview"]').click()
            desktop.locator("#queue-body tr").first.get_by_role("button", name="編輯").click()
            assert desktop.locator("#edit-dialog").is_visible()
            assert desktop.locator("#edit-order").input_value() == ""
            desktop.locator("#edit-load-catalog").click()
            desktop.get_by_text("已載入完整目錄，共 120 章").wait_for(timeout=16000)
            assert desktop.locator("#chapter-dialog").is_visible()
            assert desktop.locator("#chapter-list .chapter-item").count() == 120
            desktop.locator("#chapter-list .chapter-item").nth(1).locator('input[aria-label="製作 UUID 2"]').uncheck()
            desktop.locator("#chapter-picker-save").click()
            assert desktop.locator("#edit-excluded").input_value() == "2"
            assert desktop.locator("#excluded").input_value() == "2"
            desktop.locator("#edit-name").fill("原創測試新版")
            desktop.locator("#edit-cleaner").fill("廣告字串")
            desktop.locator("#edit-titles").fill("1 | 第一章 原創內容")
            desktop.screenshot(path=str(OUTPUT / "audio-task-editor.png"), full_page=True)
            desktop.locator("#edit-form button[type=submit]").click()
            desktop.locator("#edit-dialog").wait_for(state="hidden")
            desktop.wait_for_function("window.__issueUrls.length >= 2")
            assert any("/issues/new?" in x["url"] for x in desktop.evaluate("window.__issueUrls"))
            desktop.locator("#queue-body tr").nth(1).get_by_role(
                "button", name="確認審核並繼續").click()
            assert desktop.locator("#review-dialog").is_visible()
            review_sample={
                "schema_version":1,"artifact_role":"ad_review_candidates",
                "task_id":"book-smoke-review","worker_id":-1,
                "raw_fingerprint":"a"*64,"chapters":[1],
                "chapter_texts":{"1":"第一章 開始\n本段為測試廣告。接下來是故事。"},
                "candidates":[{"text":"本段為測試廣告","kind":"廣告","count":1,
                               "score":1,"reason":"測試廣告候選"}],
            }
            desktop.locator("#review-report-file").set_input_files({
                "name":"ad-candidates-full-book.json","mimeType":"application/json",
                "buffer":json.dumps(review_sample,ensure_ascii=False).encode("utf-8"),
            })
            desktop.get_by_text("已核對任務 book-smoke-review").wait_for()
            assert "本段為測試廣告" in desktop.locator("#review-raw").inner_text()
            desktop.locator("#review-patterns").fill("本段為測試廣告")
            assert "本段為測試廣告" not in desktop.locator("#review-clean").inner_text()
            desktop.evaluate("window.prompt=()=> '修訂廣告內容'")
            desktop.get_by_role("button",name="編輯詞句 本段為測試廣告").click()
            assert desktop.locator("#review-patterns").input_value() == "修訂廣告內容"
            assert desktop.get_by_role("button",name="從名單移除 修訂廣告內容").count() == 1
            desktop.locator("#review-manual-pattern").fill("另外手動刪除")
            desktop.locator("#review-add-manual").click()
            assert "另外手動刪除" in desktop.locator("#review-patterns").input_value()
            desktop.screenshot(path=str(OUTPUT / "audio-review-editor.png"), full_page=True)
            desktop.locator("#review-form button[type=submit]").click()
            desktop.locator("#review-dialog").wait_for(state="hidden")
            desktop.wait_for_function("window.__issueUrls.length >= 3")
            desktop.locator("#queue-body tr").first.get_by_role("button", name="進度").click()
            assert desktop.locator("#progress-dialog").is_visible()
            assert "原創測試小說" in desktop.locator("#progress-detail").inner_text()
            desktop.locator("#progress-close").click()
            desktop.locator("#queue-body tr").nth(2).get_by_role(
                "button", name="取消本次 Run").click()
            desktop.wait_for_function("window.__issueUrls.length >= 4")
            desktop.locator("#queue-body tr").first.get_by_role(
                "button", name="標為已完成").click()
            desktop.wait_for_function("window.__issueUrls.length >= 5")
            # Large chapter plans are compressed into a GitHub Issue instead of
            # failing at the prior 25k-character frontend ceiling.
            packed_result=desktop.evaluate("""async () => {
                const savedOpen=window.open;
                let opened="",redirected="";
                window.open=url=>{
                  opened=String(url);
                  return {opener:null,location:{replace:url=>redirected=String(url)},close(){}};
                };
                try{
                  const url=await window.audioIssueAction("ping",{
                    chapters:Array.from({length:8000},(_,i)=>i+1)
                  },"大型章節壓縮測試");
                  return {opened,redirected,url};
                }finally{window.open=savedOpen;}
            }""")
            assert packed_result["opened"] == "about:blank"
            assert packed_result["redirected"] == packed_result["url"]
            from urllib.parse import urlsplit,parse_qs
            import gzip,re
            from base64 import urlsafe_b64decode
            assert len(packed_result["url"])<25000
            issue_body=parse_qs(urlsplit(packed_result["url"]).query)["body"][0]
            encoded=re.search(r"AUDIO-COMMAND-V1:([A-Za-z0-9_-]+):AUDIO-COMMAND-V1",issue_body).group(1)
            raw=urlsafe_b64decode(encoded+"="*((-len(encoded))%4))
            command=json.loads(gzip.decompress(raw))
            assert len(command["data"]["chapters"])==8000
            assert not mock_writes, "Pages must not send a privileged GitHub API PUT"
            desktop.locator('[data-tab="runs"]').click()
            assert desktop.locator("#runs").is_visible()
            mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1,
                                      is_mobile=True, has_touch=True)
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            # Validate populated queue controls on mobile, not merely a disconnected shell.
            mobile.route("https://api.github.com/**", github_mock)
            mobile.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded", timeout=20000)
            mobile.locator("#queue-body tr").first.get_by_text("原創測試小說").wait_for()
            assert mobile.locator("#queue-body tr").count() == 3
            assert mobile.locator("#count-queued").inner_text() == "1"
            # Every task status and action must fit the phone viewport without
            # sideways scrolling or clipping the rightmost table columns.
            bounds=mobile.locator("#queue-body tr").first.bounding_box()
            assert bounds is not None and bounds["x"]>=0
            assert bounds["x"]+bounds["width"]<=391
            assert mobile.locator("#queue-body tr").nth(2).get_by_role(
                "button",name="取消本次 Run").is_visible()
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
