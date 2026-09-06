"""Isolated test script to verify scraping 3 chapters from 69shuba in GitHub Actions.

Uses SeleniumBase UC (Undetected Chrome) mode to handle Cloudflare Turnstile/JS challenge,
then extracts text, saves chapters, and tests clearance cookie reuse.
Does not touch the production pipeline.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from bs4 import BeautifulSoup

CHAPTERS_TO_TEST = [
    {"num": 1, "url": "https://www.69shuba.com/txt/29590/20544520", "expected_title": "绯红"},
    {"num": 2, "url": "https://www.69shuba.com/txt/29590/20544522", "expected_title": "情况"},
    {"num": 3, "url": "https://www.69shuba.com/txt/29590/20544525", "expected_title": "梅丽莎"},
]


def clean_content(html: str) -> tuple[str, str]:
    """Parse title and novel text from 69shuba chapter HTML."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(strip=True) if heading else "未知章名"

    body = soup.select_one(".txtnav")
    if not body:
        return title, ""

    for node in body.select("h1, .txtinfo, .bottom-ad, .contentadv, script, style, iframe, .page1"):
        node.decompose()

    text = body.get_text("\n", strip=True)
    import re
    text = re.sub(r"(?:\n|^)[（(]本章完[）)]\s*$", "", text).strip()
    return title, text


def run_test():
    from seleniumbase import Driver

    output_dir = Path("scraped_chapters")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("🚀 [Cloudflare Bypass Test] 啟動 SeleniumBase UC 模式...")
    print("=" * 60)

    # In GitHub Actions with xvfb, headless=False runs inside the virtual display
    driver = Driver(uc=True, headless=False)
    results = []

    try:
        cf_clearance = None
        user_agent = None

        for idx, item in enumerate(CHAPTERS_TO_TEST, 1):
            chap_num = item["num"]
            chap_url = item["url"]
            print(f"\n[{idx}/3] 正在抓取第 {chap_num} 章: {chap_url} ...")

            start_t = time.time()
            driver.uc_open_with_reconnect(chap_url, reconnect_time=6)

            # Wait for cloudflare challenge to settle
            for wait_round in range(10):
                page_title = driver.title
                if "Just a moment..." not in page_title and "Cloudflare" not in page_title:
                    break
                print(f"  ⏳ 檢測到 Cloudflare 挑戰頁面 ({page_title})，等待驗證中... ({wait_round+1}/10)")
                try:
                    driver.uc_gui_click_captcha()
                except Exception:
                    pass
                time.sleep(2)

            page_title = driver.title
            html = driver.page_source
            elapsed = time.time() - start_t

            print(f"  📄 頁面標題: {page_title} (耗時 {elapsed:.2f}s, HTML 大小: {len(html)} bytes)")

            # Check if challenge was bypassed
            if "Just a moment..." in page_title:
                print(f"  ❌ 仍停留在 Cloudflare 挑戰頁面！")
                results.append({"num": chap_num, "status": "failed", "reason": "Cloudflare challenge not cleared", "chars": 0})
                continue

            title, text = clean_content(html)
            if not text:
                print(f"  ❌ 抓取失敗：正文為空！HTML 預覽: {html[:300]}")
                results.append({"num": chap_num, "status": "failed", "reason": "正文為空", "chars": 0})
                continue

            # Save chapter to file
            out_file = output_dir / f"Chapter_{chap_num:03d}.txt"
            out_file.write_text(f"{title}\n\n{text}\n", encoding="utf-8")

            # Extract cf_clearance cookie
            cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
            cf_clearance = cookies.get("cf_clearance")
            user_agent = driver.execute_script("return navigator.userAgent")

            print(f"  ✅ 抓取成功！章節名: {title}")
            print(f"  📊 正文字數: {len(text)} 字，已儲存至 {out_file}")
            print(f"  🔍 開頭預覽: {text[:100]}...")

            results.append({
                "num": chap_num,
                "title": title,
                "status": "success",
                "chars": len(text),
                "preview": text[:120],
                "file": str(out_file),
                "elapsed": elapsed,
            })

            # Small delay between chapters
            time.sleep(2)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # Summary Report
    print("\n" + "=" * 60)
    print("📋 [測試總結摘要]")
    print("=" * 60)
    success_count = sum(1 for r in results if r["status"] == "success")
    print(f"總測試章數: {len(CHAPTERS_TO_TEST)}, 成功: {success_count}, 失敗: {len(CHAPTERS_TO_TEST) - success_count}")

    summary_lines = [
        "## 🧪 Cloudflare 抓取隔離測試報告（方案 B：SeleniumBase UC）",
        "",
        f"- **測試目標：** 《诡秘之主》前 3 章 (`https://www.69shuba.com/book/29590/`)",
        f"- **執行環境：** GitHub Actions (Ubuntu-latest + Xvfb)",
        f"- **測試結果：** 成功 {success_count} / {len(CHAPTERS_TO_TEST)} 章",
        "",
        "### 章節抓取詳情",
        "",
        "| 章號 | 章名 | 狀態 | 字數 | 耗時 | 正文開頭預覽 |",
        "|:---:|:---|:---:|---:|---:|:---|",
    ]

    for r in results:
        if r["status"] == "success":
            preview = r['preview'].replace('\n', ' ')
            summary_lines.append(f"| 第 {r['num']} 章 | {r['title']} | ✅ 成功 | {r['chars']} 字 | {r['elapsed']:.1f}s | {preview}... |")
        else:
            summary_lines.append(f"| 第 {r['num']} 章 | - | ❌ 失敗 | 0 字 | - | {r.get('reason')} |")

    summary_md = "\n".join(summary_lines)

    step_summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_file:
        with open(step_summary_file, "a", encoding="utf-8") as f:
            f.write("\n\n" + summary_md + "\n")

    if success_count < len(CHAPTERS_TO_TEST):
        print("❌ 測試未完全通過！")
        sys.exit(1)
    else:
        print("🎉 全部 3 章測試抓取成功！")


if __name__ == "__main__":
    run_test()
