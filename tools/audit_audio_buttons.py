"""Audit visible and selectable AUDIO buttons in real Chromium, without GitHub navigation.

The browser runs against checked-out Pages assets and mocked network responses.
GitHub writes are never performed by this test. All navigation/popups are
recorded and treated as a hard regression, including reference links.
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"
OUTPUT = ROOT / "ui-test-screenshots"
REPORT = OUTPUT / "audio-button-audit.json"

STATE = {
    "schema_version": 3, "revision": 5, "queue": [
        {"task_id": "book-audit-paused", "book_title": "測試小說 A", "catalog_url": "https://czbooks.net/n/demo",
         "start_chapter": 1, "end_chapter": 3, "excluded_chapters": [], "status": "paused",
         "stages": {}, "chapter_title_overrides": {}, "chapter_order": []},
        {"task_id": "book-audit-review", "book_title": "測試小說 B", "catalog_url": "https://czbooks.net/n/review",
         "start_chapter": 1, "end_chapter": 3, "status": "waiting_review", "scrape_run_id": 12345,
         "cover_run_id": 12346, "stages": {"scrape": {"status": "completed"}, "cover": {"status": "completed"}}},
        {"task_id": "book-audit-running", "book_title": "測試小說 C", "catalog_url": "https://czbooks.net/n/run",
         "start_chapter": 1, "end_chapter": 3, "status": "running", "run_id": 67890,
         "stages": {"processing": {"status": "running"}}}
    ],
    "completed": [
        {"task_id": "book-audit-completed", "book_title": "測試小說 D", "catalog_url": "https://czbooks.net/n/completed",
         "start_chapter": 1, "end_chapter": 3, "status": "completed", "stages": {}}
    ]
}

def main():
    browser_path = next((shutil.which(x) for x in ["google-chrome", "chromium", "chromium-browser"] if shutil.which(x)), None)
    if not browser_path:
        raise RuntimeError("Chromium not installed")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = subprocess.Popen([sys.executable, "-m", "http.server", str(port),
        "--bind", "127.0.0.1", "--directory", str(SITE)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(.4)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=browser_path,args=["--no-sandbox"])
            passed, skipped, errors, popped, navigated, submitted = [], [], [], [], [], []
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("popup", lambda p: popped.append(p.url))
            page.on("framenavigated",lambda frame: navigated.append(frame.url) if frame == page.main_frame else None)
            page.on("dialog",lambda dlg: dlg.accept())

            page.add_init_script("""window.__externalAttempts=[];
              window.open=(url)=>{window.__externalAttempts.push(String(url));return null};""")

            def github_mock(route):
                path=route.request.url
                if "/contents/audiobook-queue.json" in path:
                    obj={"content":base64.b64encode(json.dumps(STATE,ensure_ascii=False).encode()).decode(),"sha":"mock"}
                    route.fulfill(status=200,content_type="application/json",body=json.dumps(obj))
                elif "/contents/audiobook-book-profiles.json" in path:
                    obj={"content":base64.b64encode(b'{"schema_version":4,"revision":0,"books":{}}').decode(),"sha":"profiles"}
                    route.fulfill(status=200,content_type="application/json",body=json.dumps(obj))
                elif "/actions/runs" in path:
                    obj={"workflow_runs":[]} if path.endswith("/actions/runs?per_page=35") else {
                       "id":67890,"name":"測試執行","status":"completed","conclusion":"success","created_at":"2026-11-27T00:00:00Z"
                    }
                    route.fulfill(status=200,content_type="application/json",body=json.dumps(obj))
                else:
                    route.fulfill(status=404,content_type="application/json",body='{"message":"Mock Not Found"}')
            page.route("https://api.github.com/**",github_mock)
            page.route("https://raw.githubusercontent.com/**/audiobook-queue.json**",
                lambda route:route.fulfill(status=200,content_type="application/json",
                    body=json.dumps(STATE,ensure_ascii=False),
                    headers={"Access-Control-Allow-Origin":"*"}))
            page.route("**/full-novels.json",lambda route:route.fulfill(status=200,content_type="application/json",
                body=json.dumps({"novels":[{"title":"測試小說 A","author":"測試作者","catalog_url":"https://69shuba.com/book/123/"}]},ensure_ascii=False)))
            page.route("https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-novel-search**",
                lambda route:route.fulfill(status=200,content_type="application/json",
                    headers={"Access-Control-Allow-Origin":"*"},
                    body=json.dumps({"keyword":"測試","sources":[{"source_id":"czbooks","source_name":"小說狂人","status":"ok","results":[{"title":"測試小說 A"}]}],
                      "results":[{"source_id":"czbooks","title":"測試小說 A","author":"測試作者","catalog_url":"https://czbooks.net/n/demo"}]},ensure_ascii=False)))

            def command_mock(route):
                path=route.request.url.split("?",1)[0]
                if path.endswith("/submit"):
                    submitted.append(route.request.post_data_json)
                    data={"ok":True,"request":{"request_id":str(uuid.uuid4()),"status":"pending"}}
                    status=202
                elif path.endswith("/status"):
                    data={"requests":[]};status=200
                elif path.endswith("/upload"):
                    data={"cover_key":"00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000002.jpg"}
                    status=200
                else:
                    data={"error":"Unsupported mock"};status=404
                route.fulfill(status=status,content_type="application/json",
                    headers={"Access-Control-Allow-Origin":"*"},body=json.dumps(data))
            page.route("https://unapiwajmgqqtdkixaab.supabase.co/functions/v1/audio-web-commands/**",command_mock)
            page.route("https://github.com/**",lambda route:route.abort() if route.request.resource_type=="document" else route.continue_())
            page.goto(f"http://127.0.0.1:{port}/",wait_until="domcontentloaded",timeout=20000)
            page.locator("#queue-body tr").first.get_by_text("測試小說 A").wait_for(timeout=15000)
            page.evaluate("""() => {
              window.audioAuth.ensure=async()=>{};
              window.audioAuth.token=async()=>"mock-user-token";
            }""")
            def verify_stayed(control):
                urls=page.evaluate("window.__externalAttempts")
                if urls: raise AssertionError(f"{control}: window.open called with {urls}")
                if popped: raise AssertionError(f"{control}: popup created {popped}")
                foreign=[x for x in navigated if urlparse(x).hostname not in ("127.0.0.1",)]
                if foreign: raise AssertionError(f"{control}: navigated to {foreign}")
            def attempt_button(selector, *args):
                locator=page.locator(selector).first
                if locator.count()==0:
                    skipped.append({"control":selector,"reason":"not rendered"});return
                if not locator.is_visible():
                    skipped.append({"control":selector,"reason":"hidden"});return
                if not locator.is_enabled():
                    skipped.append({"control":selector,"reason":"disabled"});return
                try:
                    locator.click(timeout=3500)
                    page.wait_for_timeout(80)
                    verify_stayed(selector)
                    passed.append(selector)
                except Exception as exc:
                    if "window.open" in str(exc) or "navigated" in str(exc) or "popup created" in str(exc):raise
                    skipped.append({"control":selector,"reason":str(exc).splitlines()[0][:140]})
                finally:
                    page.evaluate("""() => {
                       document.querySelectorAll("dialog[open]").forEach(d=>d.close());
                    }""")
            # Overview controls: register a pending selection first.
            attempt_button('#refresh-btn')
            attempt_button('#audio-operations-show')
            attempt_button('#dispatch-btn')
            page.locator("#queue-body tr").first.locator('input[type="checkbox"]').check()
            for name in ["batch-up","batch-down","batch-pause","batch-resume",
                         "batch-complete","batch-delete","batch-export","batch-cover-btn"]:
                attempt_button("#"+name)
            page.locator('[data-tab="create"]').click()
            attempt_button("#novel-search-go")  # empty query should fail locally, never navigate
            page.locator("#novel-search-keyword").fill("測試")
            attempt_button("#novel-search-go")
            page.locator("#novel-search-body tr").first.get_by_text("測試小說 A").wait_for(timeout=10000)
            for name in ["novel-search-select-all","novel-search-select-none"]:
                attempt_button("#"+name)
            attempt_button("#novel-search-select-all")
            attempt_button("#novel-search-stage")
            for name in ["novel-search-edit-one","novel-search-add-queue"]:
                attempt_button("#"+name)
            page.locator("#catalog-url").fill("https://czbooks.net/n/demo")
            page.locator("#source").select_option("czbooks")
            page.locator("#book-title").fill("驗收小說")
            attempt_button("#parse-btn")
            page.locator("#batch-urls").fill("驗收小說 | https://czbooks.net/n/demo | 1 | 3")
            attempt_button("#batch-btn")
            for name in ["rank-select-all","rank-select-none","rank-select-all","rank-copy","rank-add"]:
                attempt_button("#"+name)
            page.evaluate("""() => {
              window.parsedCatalog={
                catalog_url:"https://czbooks.net/n/demo",book_title:"測試小說 A",
                total_chapters:3,chapter_titles:["第 1 章","第 2 章","第 3 章"],
                chapter_parts:[{n:"1",d:"第 1 章",c:""},{n:"2",d:"第 2 章",c:""},{n:"3",d:"第 3 章",c:""}]
              };
              document.querySelector("#chapter-selection-tools").classList.remove("hidden");
            }""")
            attempt_button("#open-chapter-picker")
            for name in ["chapter-all","chapter-none","chapter-invert","chapter-sort-number",
                         "chapter-sort-original","chapter-move-up","chapter-move-down",
                         "chapter-picker-cancel","chapter-picker-save"]:
                attempt_button("#"+name)
            for item in page.locator('button[data-publish-day]').all():
                try:
                    if item.is_visible() and item.is_enabled():
                        item.click(timeout=2500);verify_stayed("schedule-time preset")
                        passed.append("schedule-time preset")
                except Exception as exc:
                    skipped.append({"control":"schedule-time preset","reason":str(exc).splitlines()[0][:140]})
            page.locator('[data-tab="runs"]').click()
            attempt_button('#refresh-btn')
            # The links must open the internal reference dialog, never Github.
            external_links=page.locator('a[href^="https://github.com/"]')
            if external_links.count():
                link=external_links.first
                link.click(timeout=4000)
                assert page.locator("#audio-external-details").is_visible()
                verify_stayed("external GitHub link")
                page.locator("#audio-reference-close").click()
                passed.append("in-page Github link")
            page.locator('[data-tab="overview"]').click()
            # Open manual cover: backend upload and command submission are mocked.
            attempt_button('#queue-body tr:first-child button:has-text("手動封面")')
            from io import BytesIO
            from PIL import Image
            jpeg = BytesIO()
            Image.new("RGB",(1440,800),(190,205,225)).save(jpeg,format="JPEG")
            page.locator("#queue-body tr").first.get_by_role("button",name="手動封面").click()
            page.locator("#manual-cover-file").set_input_files({
                "name":"test-cover.jpg","mimeType":"image/jpeg","buffer":jpeg.getvalue()})
            page.locator("#manual-cover-submit").wait_for(state="visible")
            page.wait_for_function("!document.querySelector('#manual-cover-submit').disabled",timeout=10000)
            attempt_button("#manual-cover-submit")
            page.wait_for_timeout(300)
            verify_stayed("manual cover upload")
            # All actual queue mutations use the same internal API.
            assert any(x.get("op")=="manual_cover" for x in submitted), "Manual cover was not sent to backend"
            page.locator('#queue-body tr').first.locator('input[type="checkbox"]').check()
            page.locator("#batch-cover-btn").click()
            attempt_button("#batch-cover-generate")
            page.wait_for_timeout(200)
            verify_stayed("batch-cover-generate")
            assert any(x.get("op")=="batch_cover_info" for x in submitted), "Cover research not queued"
            # Programmatic scan of every available inline action with accessible ID.
            all_ids=page.evaluate("""() => [...document.querySelectorAll("button[id]")].map(b=>({
               id:b.id,label:b.textContent.trim(),disabled:b.disabled,hidden:!(b.offsetWidth||b.offsetHeight)
            }))""")
            report={"tested_button_clicks":passed,"skipped":skipped,"buttons_in_dom":all_ids,
                    "backend_operations":[x.get("op") for x in submitted],
                    "popup_attempts":popped,"external_navigation":navigated,
                    "runtime_errors":errors}
            REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print("BUTTON_AUDIT:",json.dumps({
              "passed":passed,"skipped":skipped,"submitted":submitted,"errors":errors,
              "external":page.evaluate("window.__externalAttempts")
            },ensure_ascii=False)[:12000],flush=True)
            if errors: raise AssertionError("JS runtime errors: "+"; ".join(errors[:6]))
            verify_stayed("all browser controls")
            assert any(x.get("op") in ("enqueue","batch_add") for x in submitted), "Add to queue was not sent to backend"
            assert all(not str(x).startswith("http") for x in page.evaluate("window.__externalAttempts"))
            page.locator('[data-tab="create"]').click()
            page.screenshot(path=str(OUTPUT/"audio-all-buttons-audit-desktop.png"),full_page=True)
            phone=browser.new_page(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
            phone.route("https://api.github.com/**",github_mock)
            phone.goto(f"http://127.0.0.1:{port}/",wait_until="domcontentloaded",timeout=20000)
            phone.locator('[data-tab="create"]').click()
            phone.screenshot(path=str(OUTPUT/"audio-all-buttons-audit-mobile.png"),full_page=True)
            browser.close()
            print("AUDIO inline buttons checked:",len(passed),"skipped:",len(skipped),
                  "commands:",len(submitted),"popups:",len(popped))
    finally:
        server.terminate()

if __name__=="__main__":
    main()
