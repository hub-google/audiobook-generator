"""Helper to launch Chrome with a persistent user data profile and extract YouTube cookies."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent / "youtube_backfill_chrome_profile"
DOTENV_PATH = PROJECT_ROOT / ".env"

CHROME_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome_executable() -> Optional[str]:
    """Locate the system Chrome executable if available."""
    for path in CHROME_PATHS:
        if os.path.isfile(path):
            return path
    return None


def extract_youtube_cookies(
    profile_dir: Optional[Path] = None,
    timeout_seconds: int = 300,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """Launch Chrome with a persistent local user data profile and extract YouTube cookies.
    
    Args:
        profile_dir: Directory to store user data / session persistently.
        timeout_seconds: Max seconds to wait for user to log in and cookies to be available.
        progress_callback: Callback to report status messages.
        
    Returns:
        Formatted cookie string for HTTP requests: 'key1=val1; key2=val2; ...'
    """
    profile = profile_dir or DEFAULT_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)

    chrome_exe = find_chrome_executable()

    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)
        else:
            print(msg)

    log(f"正在啟動 Chrome 瀏覽器（使用本機持久登入設定檔：{profile.name}）…")

    with sync_playwright() as p:
        launch_kwargs = {
            "user_data_dir": str(profile),
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                "--start-maximized",
            ],
            "no_viewport": True,
        }
        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe
        else:
            launch_kwargs["channel"] = "chrome"

        context = p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()

            log("正在開啟 YouTube Studio… 若尚未登入，請在 Chrome 視窗內完成登入。")
            try:
                page.goto("https://studio.youtube.com", timeout=60000)
            except Exception as exc:
                log(f"頁面載入提醒: {exc}")

            start_time = time.time()
            cookie_string = ""
            last_prompt_time = 0
            navigated_to_studio = False

            while time.time() - start_time < timeout_seconds:
                # Find active pages
                pages = [p for p in context.pages if not p.is_closed()]
                if not pages:
                    break

                target_page = None
                for p in pages:
                    try:
                        u = p.url
                        if "studio.youtube.com" in u and "accounts.google.com" not in u and "signin" not in u:
                            target_page = p
                            break
                    except Exception:
                        pass

                if not target_page:
                    target_page = pages[-1]

                current_url = ""
                try:
                    current_url = target_page.url if target_page else ""
                except Exception:
                    pass

                # Check cookies in context
                try:
                    cookies = context.cookies()
                except Exception:
                    cookies = []

                # Group cookies by name, preferring youtube.com domains
                cookie_dict: dict[str, str] = {}
                for c in sorted(cookies, key=lambda x: (1 if "youtube.com" in x.get("domain", "") else 0)):
                    name = c.get("name")
                    val = c.get("value")
                    if name and val:
                        cookie_dict[name] = val

                has_sapisid = bool(
                    cookie_dict.get("SAPISID")
                    or cookie_dict.get("__Secure-3PAPISID")
                    or cookie_dict.get("__Secure-1PAPISID")
                )
                has_sid = bool(
                    cookie_dict.get("SID")
                    or cookie_dict.get("__Secure-3PSID")
                    or cookie_dict.get("__Secure-1PSID")
                    or cookie_dict.get("LOGIN_INFO")
                )

                is_on_signin = "accounts.google.com" in current_url or "signin" in current_url
                is_on_studio = "studio.youtube.com" in current_url and not is_on_signin

                if has_sapisid and has_sid:
                    if not is_on_studio:
                        if not navigated_to_studio:
                            log("偵測到 Google 帳號已通過驗證，正在自動跳轉至 YouTube Studio…")
                            try:
                                target_page.goto("https://studio.youtube.com", timeout=30000)
                                navigated_to_studio = True
                            except Exception:
                                pass
                    else:
                        log("✅ 成功偵測到 YouTube Studio 已登入！正在完成憑證儲存…")
                        time.sleep(1.0)
                        try:
                            cookies = context.cookies()
                        except Exception:
                            pass
                        for c in sorted(cookies, key=lambda x: (1 if "youtube.com" in x.get("domain", "") else 0)):
                            name = c.get("name")
                            val = c.get("value")
                            if name and val:
                                cookie_dict[name] = val

                        # Extract document.cookie if available
                        if target_page:
                            try:
                                doc_cookie = target_page.evaluate("() => document.cookie")
                                if doc_cookie:
                                    for part in doc_cookie.split(";"):
                                        if "=" in part:
                                            k, v = part.strip().split("=", 1)
                                            if k and v and k not in cookie_dict:
                                                cookie_dict[k] = v
                            except Exception:
                                pass

                        cookie_parts = [f"{name}={val}" for name, val in cookie_dict.items()]
                        cookie_string = "; ".join(cookie_parts)
                        log("✅ 已成功提取 YouTube Studio 憑證！正在自動關閉 Chrome 視窗…")
                        break

                if is_on_signin:
                    now = time.time()
                    if now - last_prompt_time > 8:
                        log("⚠️ 請在開啟的 Chrome 視窗內完成 Google 登入以獲取最新 YouTube Studio 權限…")
                        last_prompt_time = now

                time.sleep(1.0)
        finally:
            try:
                context.close()
            except Exception:
                pass

        if not cookie_string:
            raise TimeoutError(f"在 {timeout_seconds} 秒內未完成 YouTube Studio 登入或未偵測到有效 Cookie，請重試。")

        return cookie_string


def save_cookie_to_env(cookie_str: str, env_path: Path = DOTENV_PATH) -> None:
    """Save or update YOUTUBE_STUDIO_COOKIES in .env file and active os.environ."""
    lines: list[str] = []
    found = False
    clean_val = cookie_str.strip()
    os.environ["YOUTUBE_STUDIO_COOKIES"] = clean_val
    clean_cookie = clean_val.replace('"', '\\"')
    new_entry = f'YOUTUBE_STUDIO_COOKIES="{clean_cookie}"\n'

    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        for line in content.splitlines(keepends=True):
            if re.match(r"^\s*YOUTUBE_STUDIO_COOKIES\s*=", line):
                lines.append(new_entry)
                found = True
            else:
                lines.append(line)

    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(new_entry)

    env_path.write_text("".join(lines), encoding="utf-8")


class BrowserCardWorker:
    """Persistent Playwright browser worker to batch-set info cards inside YouTube Studio."""

    def __init__(self, profile_dir: Optional[Path] = None, headless: bool = False) -> None:
        self.profile_dir = profile_dir or DEFAULT_PROFILE_DIR
        self.headless = headless
        self._pw = None
        self._context = None
        self._page = None

    def _has_studio_auth(self) -> bool:
        if not self._context:
            return False
        try:
            pages = [p for p in self._context.pages if not p.is_closed()]
            if not pages:
                return False
            
            # Find any page on studio
            has_studio_page = any(
                "studio.youtube.com" in p.url and "accounts.google.com" not in p.url and "signin" not in p.url
                for p in pages
            )
            if not has_studio_page:
                return False

            cookies = self._context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
            has_sapisid = bool(
                cookie_dict.get("SAPISID")
                or cookie_dict.get("__Secure-3PAPISID")
                or cookie_dict.get("__Secure-1PAPISID")
            )
            has_sid = bool(
                cookie_dict.get("SID")
                or cookie_dict.get("__Secure-3PSID")
                or cookie_dict.get("__Secure-1PSID")
                or cookie_dict.get("LOGIN_INFO")
            )
            return has_sapisid and has_sid
        except Exception:
            return False

    def ensure_started(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        if self._page and not self._page.is_closed() and self._has_studio_auth():
            return
        if progress_callback:
            progress_callback("正在啟動 Chrome 瀏覽器環境（用於安全掛載資訊卡）…")
        if not self._pw:
            self._pw = sync_playwright().start()
        chrome_exe = find_chrome_executable()
        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                "--start-maximized",
            ],
            "no_viewport": True,
        }
        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe
        else:
            launch_kwargs["channel"] = "chrome"

        if not self._context:
            self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        if "studio.youtube.com" not in self._page.url:
            try:
                self._page.goto("https://studio.youtube.com", timeout=45000)
            except Exception as exc:
                if progress_callback:
                    progress_callback(f"Studio 首頁載入提示: {exc}")

        # Check authentication; if not logged in, wait for user
        if not self._has_studio_auth():
            if progress_callback:
                progress_callback("⚠️ 偵測到 YouTube Studio 尚未登入，請在開啟的 Chrome 視窗內完成登入…")
            start_wait = time.time()
            while time.time() - start_wait < 180:
                if self._has_studio_auth():
                    current_url = ""
                    try:
                        current_url = self._page.url
                    except Exception:
                        pass
                    if "studio.youtube.com" not in current_url:
                        try:
                            self._page.goto("https://studio.youtube.com", timeout=30000)
                        except Exception:
                            pass
                    # Sync new cookies to .env
                    cookies = self._context.cookies()
                    cookie_dict = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
                    cookie_parts = [f"{name}={val}" for name, val in cookie_dict.items()]
                    save_cookie_to_env("; ".join(cookie_parts))
                    if progress_callback:
                        progress_callback("✅ 偵測到 YouTube Studio 已登入！繼續執行資訊卡掛載…")
                    break
                time.sleep(1.5)

            if not self._has_studio_auth():
                raise TimeoutError("等待 YouTube Studio 登入逾時，請在 Chrome 中登入後再試。")

    def set_card(
        self,
        video_id: str,
        playlist_id: str,
        first_video_id: str = "",
        card1_ms: int = 3000,
        card2_ms: int = 13000,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        self.ensure_started(progress_callback)
        js_code = """
        async ({ videoId, playlistId, firstVideoId, card1Ms, card2Ms }) => {
            function getCookie(name) {
                const match = document.cookie.match(new RegExp('(^|;\\\\s*)' + name + '=([^;]*)'));
                return match ? decodeURIComponent(match[2]) : '';
            }

            const sapisid = getCookie("SAPISID") || getCookie("__Secure-3PAPISID") || getCookie("__Secure-1PAPISID");
            const origin = "https://studio.youtube.com";
            
            let authHeader = "";
            if (sapisid) {
                const now = Math.floor(Date.now() / 1000);
                const msg = `${now} ${sapisid} ${origin}`;
                const enc = new TextEncoder();
                const hashBuf = await crypto.subtle.digest("SHA-1", enc.encode(msg));
                const hashHex = Array.from(new Uint8Array(hashBuf)).map(b => b.toString(16).padStart(2, '0')).join('');
                authHeader = `SAPISIDHASH ${now}_${hashHex}`;
            }

            let apiKey = window.ytcfg?.get("INNERTUBE_API_KEY") || "";
            let clientVersion = window.ytcfg?.get("INNERTUBE_CLIENT_VERSION") || "1.20260826.03.00";
            let pageId = window.ytcfg?.get("DELEGATED_SESSION_ID") || "";
            let channelId = window.ytcfg?.get("CHANNEL_ID") || "UCIUtGUZ24fMsfzZtydQTsPg";
            let authUser = window.ytcfg?.get("SESSION_INDEX") || "0";
            let identityToken = window.ytcfg?.get("ID_TOKEN") || "";
            
            let delegationContext = {
                roleType: { channelRoleType: "CREATOR_CHANNEL_ROLE_TYPE_OWNER" },
                externalChannelId: channelId
            };
            
            let infoCards = [];
            if (firstVideoId && firstVideoId !== videoId) {
                let card1 = {
                    videoId: videoId,
                    teaserStartMs: card1Ms,
                    videoInfoCard: { videoId: firstVideoId },
                    infoCardEntityId: String(Date.now()),
                    customMessage: "第一次收聽？從第一集開始",
                    teaserText: "第一次收聽？從第一集開始"
                };
                infoCards.push(card1);
            }
            
            let card2 = {
                videoId: videoId,
                teaserStartMs: card2Ms,
                playlistInfoCard: { fullPlaylistId: playlistId },
                infoCardEntityId: String(Date.now() + 1),
                customMessage: "完整小說播放清單",
                teaserText: "完整小說播放清單"
            };
            infoCards.push(card2);
            
            let payload = {
                context: {
                    client: {
                        clientName: 62,
                        clientVersion: clientVersion,
                        hl: "zh-TW",
                        gl: "TW"
                    },
                    request: { returnLogEntry: true, internalExperimentFlags: [] },
                    user: {
                        ...(pageId ? { onBehalfOfUser: pageId } : {}),
                        delegationContext: delegationContext,
                        serializedDelegationContext: ""
                    }
                },
                delegationContext: delegationContext,
                externalVideoId: videoId,
                infoCardEdit: {
                    infoCards: infoCards
                }
            };
            
            let headers = {
                "Content-Type": "application/json",
                "X-Youtube-Client-Name": "62",
                "X-Youtube-Client-Version": clientVersion,
                "X-Goog-AuthUser": authUser,
            };
            if (authHeader) {
                headers["Authorization"] = authHeader;
            }
            if (pageId) {
                headers["X-Goog-PageId"] = pageId;
            }
            if (identityToken) {
                headers["X-Youtube-Identity-Token"] = identityToken;
            }
            
            let url = "/youtubei/v1/video_editor/edit_video?alt=json" + (apiKey ? "&key=" + apiKey : "");
            let res = await fetch(url, {
                method: "POST",
                credentials: "include",
                headers: headers,
                body: JSON.stringify(payload)
            });
            
            let json = {};
            try {
                json = await res.json();
            } catch (e) {
                json = { parseError: String(e) };
            }
            return { ok: res.ok, status: res.status, data: json };
        }
        """
        args = {
            "videoId": video_id,
            "playlistId": playlist_id,
            "firstVideoId": first_video_id,
            "card1Ms": card1_ms,
            "card2Ms": card2_ms,
        }
        result = self._page.evaluate(js_code, args)
        if not result.get("ok"):
            err_data = result.get("data", {})
            raise RuntimeError(f"瀏覽器發送資訊卡失敗 (HTTP {result.get('status')}): {err_data}")

        ext = (result.get("data") or {}).get("responseContext", {}).get("webResponseContextExtensionData", {})
        if ext.get("challenge"):
            if progress_callback:
                progress_callback("⚠️ Google 要求身分驗證！請在開啟的 Chrome 視窗內點選確認/完成驗證（最多等待 90 秒）…")
            start_wait = time.time()
            challenge_cleared = False
            while time.time() - start_wait < 90:
                time.sleep(3)
                result = self._page.evaluate(js_code, args)
                if not result.get("ok"):
                    continue
                ext = (result.get("data") or {}).get("responseContext", {}).get("webResponseContextExtensionData", {})
                if not ext.get("challenge"):
                    challenge_cleared = True
                    if progress_callback:
                        progress_callback("✅ Google 身分驗證已通過！繼續掛載資訊卡…")
                    break
            if not challenge_cleared:
                raise RuntimeError("Google Studio 身分驗證逾時，請在開啟的 Chrome 視窗內確認登入與驗證後再試。")
        return True

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._pw = None


if __name__ == "__main__":
    print("=== [1/3] 測試 Chrome 自動擷取 YouTube Cookie ===")
    try:
        ck = extract_youtube_cookies(timeout_seconds=180)
        print(f"✅ 成功擷取 Cookie！長度: {len(ck)}")
        save_cookie_to_env(ck)
        print("✅ 已成功儲存至 .env")

        print("\n=== [2/3] 測試連線 YouTube Studio 讀取播放清單 ===")
        try:
            from youtube_backfill_gui import StudioPrivateClient
        except ImportError:
            from tools.youtube_backfill_gui import StudioPrivateClient
        client = StudioPrivateClient(ck)
        playlists = client.list_playlists()
        print(f"✅ 成功讀取到 {len(playlists)} 個播放清單：")
        for p in playlists[:5]:
            print(f"  - [{p.item_count} 支] {p.title} (ID: {p.playlist_id})")

        if playlists:
            first_pl = playlists[0]
            print(f"\n=== [3/3] 測試讀取播放清單「{first_pl.title}」影片列表 ===")
            videos = client.list_playlist_videos(first_pl.playlist_id)
            print(f"✅ 成功讀取到 {len(videos)} 支影片：")
            for v in videos[:5]:
                print(f"  - 第 {v.position + 1} 集: {v.title} (ID: {v.video_id})")
            print("\n🎉 全部驗證成功！可直接啟動 youtube_backfill_gui.py 開始批次操作資訊卡！")
    except Exception as err:
        print("❌ 測試發生錯誤:", err)

