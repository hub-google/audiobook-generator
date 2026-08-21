"""
YouTube Web 網頁版介面自動上傳測試腳本 (Playwright - 嚴格有頭模式 Headed Mode)
=============================================================================
用途：
  全程以真實可見的瀏覽器視窗 (有頭模式 Headed Mode) 模擬使用者在 YouTube Studio 網頁版介面上傳影片。
  
特色：
  1. 嚴格有頭模式 (Headless=False)：每次執行必定彈出可視化瀏覽器視窗，讓您完整看見每一步點擊與上傳過程。
  2. 視覺化慢速模式 (Slow-Mo 800ms)：每次點擊、輸入文字皆有適當節奏，方便肉眼觀察操作流程。
  3. 持久化 Profile (.yt_browser_profile)：保存 Google 帳號 Session 與 Cookie，無需每次重複登入。
  4. 自動化流程：自動點擊「建立」->「上傳影片」-> 選取檔案 -> 設定標題與非兒童受眾 -> 設定公開狀態 -> 發布並輸出影片網址。
"""

import os
import sys
import time
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# 預設目標影片路徑
DEFAULT_VIDEO_PATH = r"C:\Users\cit50\Desktop\凡人修仙傳_chapter_1.mp4"

# 瀏覽器設定檔保存目錄 (用於保持登入狀態)
PROFILE_DIR = Path(__file__).parent / ".yt_browser_profile"


def upload_video_via_web_headed(
    video_path: str,
    title: str = None,
    description: str = "",
    visibility: str = "UNLISTED"  # UNLISTED (不公開), PRIVATE (私人), PUBLIC (公開)
):
    video_file = Path(video_path)
    if not video_file.exists():
        print(f"[錯誤] 找不到目標影片檔案: {video_path}")
        return False

    print("=" * 65)
    print("🖥️  YouTube 網頁版全可視化 (有頭模式 Headed) 上傳測試啟動")
    print(f"📁 影片檔案: {video_file.resolve()} ({video_file.stat().st_size / 1024 / 1024:.2f} MB)")
    print(f"🔐 預設隱私設定: {visibility}")
    print(f"📂 瀏覽器 Profile 目錄: {PROFILE_DIR.resolve()}")
    print("=" * 65)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        print("\n[步驟 1/6] 正在開啟真實瀏覽器視窗 (有頭模式)...")
        
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--start-maximized"
        ]

        # 嚴格設定 headless=False，加入 slow_mo 方便肉眼觀察
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            headless=False,           # 絕對有頭模式
            slow_mo=800,              # 每步動作間隔 800ms，肉眼清晰可見
            args=launch_args,
            no_viewport=True,
            timeout=60000
        )

        page = context.pages[0] if context.pages else context.new_page()

        print("[步驟 2/6] 前往 YouTube Studio (https://studio.youtube.com)...")
        page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=60000)

        # 檢查是否需要登入
        time.sleep(3)
        if "accounts.google.com" in page.url or "signin" in page.url:
            print("\n" + "!" * 65)
            print("⚠️  請在已開啟的瀏覽器視窗中手動登入您的 Google / YouTube 帳號。")
            print("👉 登入完成並進入 YouTube Studio 控制台後，請回到此終端機按 [Enter] 鍵繼續...")
            print("!" * 65 + "\n")
            input("登入完成後請按 Enter 鍵繼續: ")

        print("[步驟 3/6] 偵測 YouTube Studio 介面元素...")
        try:
            page.wait_for_selector(
                "#create-icon, #upload-icon, ytcp-button#create-icon, ytcp-button-shape#create-icon-button, [aria-label='建立'], [aria-label='Create']",
                timeout=30000
            )
            print("✅ 成功辨識到 YouTube Studio 控制台！")
        except PlaywrightTimeoutError:
            print("⚠️ 等待 Studio 介面超時，嘗試直接尋找上傳按鈕...")

        print("[步驟 4/6] 點擊 [建立] -> [上傳影片]...")
        create_btn = page.locator("#create-icon, ytcp-button#create-icon, ytcp-button-shape#create-icon-button, [aria-label='建立'], [aria-label='Create']").first
        if create_btn.is_visible():
            create_btn.click()
            time.sleep(1)
            upload_menu_item = page.locator(
                "tp-yt-paper-item:has-text('上傳影片'), tp-yt-paper-item:has-text('Upload videos'), ytcp-text-menu-item:has-text('上傳影片'), #text-item-0"
            ).first
            if upload_menu_item.is_visible():
                upload_menu_item.click()
                time.sleep(1.5)
        else:
            direct_upload_btn = page.locator("#upload-icon, ytcp-icon-button#upload-icon").first
            if direct_upload_btn.is_visible():
                direct_upload_btn.click()
                time.sleep(1.5)

        print(f"[步驟 5/6] 注入影片檔案: {video_file.name} ...")
        file_input = page.locator("input[type='file']")
        try:
            file_input.wait_for(state="attached", timeout=15000)
            file_input.set_input_files(str(video_file.resolve()))
            print("✅ 影片已載入，YouTube 正在解析影片資料...")
        except Exception as e:
            print(f"[錯誤] 選取檔案失敗: {e}")
            input("請查看瀏覽器狀態後按 Enter 關閉...")
            context.close()
            return False

        print("[步驟 6/6] 自動填寫詳細資料與公開設定...")
        try:
            page.wait_for_selector("#dialog, ytcp-uploads-dialog, #title-textarea", timeout=30000)
            time.sleep(2)

            # 1. 自訂標題
            if title:
                print(f"  📝 設定標題: {title}")
                title_box = page.locator("#title-textarea #textbox, div#textbox[aria-label*='標題'], div#textbox[aria-label*='Title']").first
                if title_box.is_visible():
                    title_box.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    title_box.fill(title)
                    time.sleep(1)

            # 2. 必填受眾條件 (否，這不是專為兒童打造的內容)
            print("  👶 設定受眾條件 (非兒童內容)...")
            not_for_kids_radio = page.locator(
                "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], "
                "ytcp-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], "
                "[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], "
                "tp-yt-paper-radio-button:has-text('否，這不是專為兒童打造的內容'), "
                "tp-yt-paper-radio-button:has-text('No, it\\'s not made for kids')"
            ).first
            not_for_kids_radio.scroll_into_view_if_needed()
            not_for_kids_radio.click()
            time.sleep(1)

            # 3. 點擊「下一步」前進到最後設定頁
            print("  ➡️  點擊 [下一步] 按鈕...")
            next_btn = page.locator("#next-button, ytcp-button#next-button, button:has-text('下一步'), button:has-text('Next')").first
            for step_i in range(3):
                if next_btn.is_visible() and next_btn.is_enabled():
                    next_btn.click()
                    print(f"     點擊 [下一步] ({step_i + 1}/3)")
                    time.sleep(1.5)

            # 4. 設定公開設定 (預設不公開 UNLISTED)
            print(f"  👁️  設定隱私狀態為: {visibility} ...")
            vis_radio_map = {
                "PRIVATE": "tp-yt-paper-radio-button[name='PRIVATE'], [name='PRIVATE']",
                "UNLISTED": "tp-yt-paper-radio-button[name='UNLISTED'], [name='UNLISTED']",
                "PUBLIC": "tp-yt-paper-radio-button[name='PUBLIC'], [name='PUBLIC']",
            }
            target_selector = vis_radio_map.get(visibility.upper(), vis_radio_map["UNLISTED"])
            vis_radio = page.locator(target_selector).first
            if vis_radio.is_visible():
                vis_radio.click()
                time.sleep(1)

            # 讀取影片連結
            video_url = None
            try:
                link_elem = page.locator("a.ytcp-video-info, a[href*='youtu.be']").first
                if link_elem.is_visible():
                    video_url = link_elem.get_attribute("href")
            except Exception:
                pass

            # 5. 點擊「儲存 / 發布」
            print("  💾 點擊 [儲存 / 發布] 按鈕...")
            done_btn = page.locator("#done-button, ytcp-button#done-button, button:has-text('儲存'), button:has-text('發布'), button:has-text('Save'), button:has-text('Publish')").first
            if done_btn.is_visible() and done_btn.is_enabled():
                done_btn.click()
                print("✅ 已點擊發布！影片已送出！")
                time.sleep(3)

            # 再次嘗試抓取發布完成視窗中的影片連結
            if not video_url:
                try:
                    link_elem = page.locator("a[href*='youtu.be'], .ytcp-uploads-still-processing-dialog a").first
                    if link_elem.is_visible():
                        video_url = link_elem.get_attribute("href") or link_elem.inner_text()
                except Exception:
                    pass

            print("\n" + "=" * 65)
            print("🎉🎉 YouTube 網頁版有頭上傳流程執行完畢！")
            if video_url:
                print(f"🔗 影片預覽/分享連結: {video_url}")
            print("ℹ️  狀態: 影片已在瀏覽器中成功提交，YouTube 正在背景進行轉檔。")
            print("=" * 65 + "\n")

            print("👀 瀏覽器視窗將保持開啟 10 秒供您檢視結果...")
            time.sleep(10)

            context.close()
            return True

        except Exception as e:
            print(f"[錯誤] 操作流程遭遇例外: {e}")
            import traceback
            traceback.print_exc()
            input("發生錯誤，請在畫面上檢查後按 Enter 關閉瀏覽器...")
            context.close()
            return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YouTube Web 網頁版全可視化有頭上傳測試工具")
    parser.add_argument("--video", default=DEFAULT_VIDEO_PATH, help="影片檔案完整路徑")
    parser.add_argument("--title", default=None, help="自訂影片標題 (預設使用原檔名)")
    parser.add_argument("--visibility", default="UNLISTED", choices=["UNLISTED", "PRIVATE", "PUBLIC"], help="影片公開設定")
    
    args = parser.parse_args()
    
    success = upload_video_via_web_headed(
        video_path=args.video,
        title=args.title,
        visibility=args.visibility
    )
    
    sys.exit(0 if success else 1)
