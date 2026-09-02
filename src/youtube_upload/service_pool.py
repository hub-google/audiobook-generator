"""YouTube API v3 Service Pool and authentication management."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .errors import UploadPaused, classify_daily_limit
from .state import (
    MAX_YOUTUBE_ACCOUNT_SLOTS,
    atomic_write_json as _atomic_write_json,
    configured_youtube_account_slots,
)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

EXIT_RETRY_LATER = 75
YOUTUBE_SLOT_ROTATION_ROUNDS = 3


def _get_symbol(name: str, fallback: Any) -> Any:
    uploader = sys.modules.get("src.youtube_api_uploader") or sys.modules.get("youtube_api_uploader")
    if uploader is not None and hasattr(uploader, name):
        return getattr(uploader, name)
    return fallback


def _find_gh() -> str:
    """Resolve GitHub CLI executable path, checking PATH then common Windows locations."""
    import shutil as _shutil
    found = _shutil.which("gh")
    if found:
        return found
    installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if installed.exists():
        return str(installed)
    raise FileNotFoundError("找不到 GitHub CLI (gh)。請先安裝並執行 gh auth login。")


class YouTubeServicePool:
    """管理多組 YouTube API 專案金鑰，支援自動探索、單一介面調用與 403 quotaExceeded 無縫輪替"""

    def __init__(self):
        self.accounts = []
        self.active_index = 0
        self.rotation_round = 1
        self.api_rotation_round = 1
        self.channel_id = None
        self._activation_pause = None
        self.discover_accounts()

    def discover_accounts(self):
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cs_dir = os.path.join(BASE_DIR, "client_secret")

        path_exists_fn = _get_symbol("os", os).path.exists

        for slot in range(1, MAX_YOUTUBE_ACCOUNT_SLOTS + 1):
            # 1. 檔案路徑檢查
            cs_path = os.path.join(cs_dir, f"client_secret_{slot}.json")
            if slot == 1 and not path_exists_fn(cs_path):
                root_cs = os.path.join(BASE_DIR, "client_secret.json")
                if path_exists_fn(root_cs):
                    cs_path = root_cs

            tok_path = os.path.join(cs_dir, f"token_{slot}.json")

            # 2. 環境變數固定使用 YOUTUBE_*_1 .. YOUTUBE_*_10。
            ref_token = os.environ.get(f"YOUTUBE_REFRESH_TOKEN_{slot}", "").strip()
            client_id = os.environ.get(f"YOUTUBE_CLIENT_ID_{slot}", "").strip()
            client_secret = os.environ.get(f"YOUTUBE_CLIENT_SECRET_{slot}", "").strip()

            has_file = (path_exists_fn(cs_path) or path_exists_fn(tok_path))
            has_env = bool((client_id and client_secret) or ref_token)

            if has_file or has_env:
                self.accounts.append({
                    "slot": slot,
                    "cs_path": cs_path,
                    "tok_path": tok_path,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": ref_token,
                    "service": None,
                    "creds": None,
                    "channel_id": None,
                    "api_exhausted": False,
                    "exhausted": False,
                    "unavailable": False,
                })

        logging.info("🔑 [YouTube-Pool] 偵測到 %s 組 YouTube API 專案金鑰/憑證設定。", len(self.accounts))

    def require_expected_accounts(self):
        """Fail early in CI when the configured credential pool is incomplete."""
        raw_expected = os.environ.get("YOUTUBE_EXPECTED_ACCOUNT_COUNT", "").strip()
        if not raw_expected:
            return
        try:
            expected = int(raw_expected)
        except ValueError as exc:
            raise RuntimeError(
                "YOUTUBE_EXPECTED_ACCOUNT_COUNT must be an integer"
            ) from exc
        if not 1 <= expected <= MAX_YOUTUBE_ACCOUNT_SLOTS:
            raise RuntimeError(
                f"YOUTUBE_EXPECTED_ACCOUNT_COUNT must be between 1 and {MAX_YOUTUBE_ACCOUNT_SLOTS}"
            )
        complete_slots = configured_youtube_account_slots()
        discovered = len(complete_slots)
        if discovered < expected:
            missing = [
                str(slot) for slot in range(1, expected + 1)
                if slot not in complete_slots
            ]
            raise RuntimeError(
                f"YouTube credential pool is incomplete: found {discovered}/{expected} "
                f"accounts; missing slots: {', '.join(missing) or 'unknown'}. "
                "Each slot needs client ID, client secret, and refresh token."
            )

    def _authenticate_account(self, acc):
        slot = acc["slot"]
        tok_path = acc["tok_path"]
        cs_path = acc["cs_path"]
        ref_token = acc["refresh_token"]
        client_id = acc["client_id"]
        client_secret = acc["client_secret"]

        creds = None

        # 1. 嘗試從 token 檔案讀取
        if tok_path and os.path.exists(tok_path):
            try:
                creds = Credentials.from_authorized_user_file(tok_path)
            except Exception as e:
                logging.warning(f"[專案 #{slot}] 無法讀取 {tok_path}: {e}")

        # 2. 嘗試從環境變數讀取
        if (not creds or not creds.valid) and ref_token and client_id and client_secret:
            creds = Credentials(
                token=None,
                refresh_token=ref_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=None,
            )

        # 3. 嘗試動態合成 client_secret 檔
        if cs_path and not os.path.exists(cs_path) and client_id and client_secret:
            try:
                cs_data = {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
                _atomic_write_json(cs_path, cs_data)
                logging.info(f"✅ [專案 #{slot}] 已由環境變數動態生成 {cs_path}")
            except Exception as e:
                logging.warning(f"[專案 #{slot}] 無法寫入 client_secret JSON: {e}")

        # 4. 重新整理 Token
        if creds and creds.refresh_token:
            try:
                creds.refresh(Request())
                if tok_path:
                    _atomic_write_json(tok_path, json.loads(creds.to_json()))
            except Exception as e:
                logging.warning(f"[專案 #{slot}] 重新整理 Refresh Token 失敗: {e}")
                if "invalid_scope" in str(e):
                    logging.info(f"🔄 [專案 #{slot}] 嘗試清除顯式 Scope 重新刷新憑證...")
                    try:
                        creds._scopes = None
                        creds.refresh(Request())
                        if tok_path:
                            _atomic_write_json(tok_path, json.loads(creds.to_json()))
                        logging.info(f"✅ [專案 #{slot}] 成功刷新 Access Token！")
                    except Exception as ex2:
                        logging.error(f"❌ [專案 #{slot}] 再次刷新失敗: {ex2}")
                        creds = None
                else:
                    creds = None

        # 5. 若無有效憑證
        if not creds or not creds.valid:
            is_ci = os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS")
            if is_ci:
                logging.warning(f"❌ [專案 #{slot}] 在 CI/CD 無頭環境中無有效憑證，跳過此專案。")
                return None, None

            if not cs_path or not os.path.exists(cs_path):
                logging.error(f"❌ [專案 #{slot}] 找不到 client_secret 檔案且未設定環境變數！")
                return None, None

            try:
                logging.info(f"🔑 [專案 #{slot}] 正在開啟瀏覽器進行 YouTube OAuth2 登入授權...")
                flow = InstalledAppFlow.from_client_secrets_file(cs_path, SCOPES)
                creds = flow.run_local_server(port=0)
                if tok_path:
                    _atomic_write_json(tok_path, json.loads(creds.to_json()))
                logging.info(f"✅ [專案 #{slot}] 授權成功！憑證已儲存至 {tok_path}")
            except Exception as e:
                logging.error(f"❌ [專案 #{slot}] 無法完成 YouTube API 授權: {e}")
                return None, None

        service = build("youtube", "v3", credentials=creds)
        return service, creds

    def _verify_account_channel(self, index):
        """Authenticate and verify one slot when it is first activated."""
        self._activation_pause = None
        acc = self.accounts[index]
        if acc.get("unavailable"):
            return False

        service = self.get_service(index)
        if service is None:
            acc["unavailable"] = True
            logging.warning(
                "⏭️ [專案 #%s] OAuth/Service 初始化失敗，標記為不可用並跳過。",
                acc["slot"],
            )
            return False

        if acc.get("channel_id") and acc["channel_id"] == self.channel_id:
            return True

        try:
            items = service.channels().list(part="id", mine=True).execute().get("items") or []
        except Exception as error:
            paused = classify_daily_limit(error)
            if paused:
                self._activation_pause = paused
                logging.warning(
                    "⏭️ [專案 #%s] 首次啟用的頻道驗證遇到配額限制，跳過此 slot。",
                    acc["slot"],
                )
                return False
            acc["unavailable"] = True
            logging.warning(
                "⏭️ [專案 #%s] 無法驗證 YouTube 頻道，標記為不可用並跳過: %s",
                acc["slot"], error,
            )
            return False

        if len(items) != 1 or not items[0].get("id"):
            acc["unavailable"] = True
            logging.warning(
                "⏭️ [專案 #%s] 未解析到唯一 YouTube 頻道，標記為不可用並跳過。",
                acc["slot"],
            )
            return False

        channel_id = str(items[0]["id"])
        if self.channel_id is not None and channel_id != self.channel_id:
            acc["unavailable"] = True
            logging.error(
                "⏭️ [專案 #%s] 登入頻道 %s，與目前頻道 %s 不同；"
                "標記為不可用並跳過。",
                acc["slot"], channel_id, self.channel_id,
            )
            return False

        acc["channel_id"] = channel_id
        if self.channel_id is None:
            self.channel_id = channel_id
        return True

    def require_same_channel(self):
        """Initialize and verify only the active slot; backups stay lazy."""
        if not self.accounts:
            return None
        acc = self.active_account
        if not self._verify_account_channel(self.active_index):
            if self._activation_pause:
                raise self._activation_pause from self._activation_pause.original_error
            raise RuntimeError(
                f"YouTube credential slot {acc['slot']} could not be initialized and verified"
            )
        logging.info(
            "✅ [YouTube-Pool] 專案 #%s 已通過頻道驗證；其餘 %s 組備援憑證將在配額輪替時才驗證。",
            acc["slot"], max(0, len(self.accounts) - 1),
        )
        return self.channel_id

    def get_service(self, slot_idx=None):
        if not self.accounts:
            logging.error("❌ 未找到任何可用的 YouTube API 專案設定！")
            sys.exit(1)

        if slot_idx is None:
            slot_idx = self.active_index

        if slot_idx >= len(self.accounts):
            return None

        acc = self.accounts[slot_idx]
        if acc["service"] is None:
            acc["service"], acc["creds"] = self._authenticate_account(acc)
        return acc["service"]

    @property
    def active_account(self):
        if 0 <= self.active_index < len(self.accounts):
            return self.accounts[self.active_index]
        return None

    @property
    def active_service(self):
        return self.get_service(self.active_index)

    def rotate_on_quota(self, error=None, *, upload=False) -> bool:
        """Rotate API calls without confusing ordinary quota with upload quota."""
        if not self.accounts:
            return False

        exhausted_key = "exhausted" if upload else "api_exhausted"
        round_attr = "rotation_round" if upload else "api_rotation_round"
        current = self.active_account
        if current:
            current[exhausted_key] = True
            logging.warning(
                "🚨 【專案 #%s 第 %s/%s 輪失敗】 (%s)",
                current["slot"], getattr(self, round_attr, 1),
                YOUTUBE_SLOT_ROTATION_ROUNDS, error or "quotaExceeded",
            )

        old_slot = current["slot"] if current else "N/A"
        while getattr(self, round_attr, 1) <= YOUTUBE_SLOT_ROTATION_ROUNDS:
            for idx, next_acc in enumerate(self.accounts):
                if next_acc.get(exhausted_key, False) or next_acc.get("unavailable", False):
                    continue
                if self._verify_account_channel(idx):
                    self.active_index = idx
                    logging.info(
                        "🔄 【多專案自動輪替】第 %s/%s 輪：已由專案 #%s "
                        "切換至專案 #%s 繼續發布！",
                        getattr(self, round_attr, 1), YOUTUBE_SLOT_ROTATION_ROUNDS,
                        old_slot, next_acc["slot"],
                    )
                    return True

                next_acc[exhausted_key] = True
                logging.warning(
                    "⚠️ 專案 #%s 第 %s/%s 輪初始化失敗，繼續下一個 slot。",
                    next_acc["slot"], getattr(self, round_attr, 1),
                    YOUTUBE_SLOT_ROTATION_ROUNDS,
                )

            if getattr(self, round_attr, 1) >= YOUTUBE_SLOT_ROTATION_ROUNDS:
                break

            setattr(self, round_attr, getattr(self, round_attr, 1) + 1)
            for acc in self.accounts:
                acc[exhausted_key] = False
                if not acc.get("unavailable", False):
                    acc["service"] = None
                    acc["creds"] = None
            logging.warning(
                "🔁 所有 slot 第 %s 輪均失敗，重新由 slot1 開始第 %s/%s 輪。",
                getattr(self, round_attr) - 1, getattr(self, round_attr),
                YOUTUBE_SLOT_ROTATION_ROUNDS,
            )

        logging.error(
            "🚨 【所有 YouTube 專案皆切換失敗】已依序完成 %s 輪，停止重試。",
            YOUTUBE_SLOT_ROTATION_ROUNDS,
        )
        return False

    def authorize_all_local(self, sync_github=True):
        """本地一次授權所有 client_secrets 並生成對應的 tokens"""
        logging.info("🚀 開始檢查並授權所有 YouTube 專案...")
        success_count = 0
        for acc in self.accounts:
            slot = acc["slot"]
            srv, creds = self._authenticate_account(acc)
            if srv and creds:
                success_count += 1
                logging.info(f"✅ 專案 #{slot} 授權有效！")
                if sync_github and creds.refresh_token and acc["cs_path"] and os.path.exists(acc["cs_path"]):
                    try:
                        with open(acc["cs_path"], "r", encoding="utf-8") as f:
                            cs_info = json.load(f).get("installed", {})
                        cid = cs_info.get("client_id", "")
                        csec = cs_info.get("client_secret", "")
                        if cid and csec:
                            gh_bin = _find_gh()
                            suffix = f"_{slot}"
                            subprocess.run([gh_bin, "secret", "set", f"YOUTUBE_CLIENT_ID{suffix}", "--body", cid], check=False)
                            subprocess.run([gh_bin, "secret", "set", f"YOUTUBE_CLIENT_SECRET{suffix}", "--body", csec], check=False)
                            subprocess.run([gh_bin, "secret", "set", f"YOUTUBE_REFRESH_TOKEN{suffix}", "--body", creds.refresh_token], check=False)
                            logging.info(f"☁️ 已自動將專案 #{slot} 憑證同步至 GitHub Secrets (YOUTUBE_*_{slot})！")
                    except Exception as e:
                        logging.warning(f"同步至 GitHub Secrets 失敗: {e}")
        logging.info(f"🎉 授權檢測完成：共 {success_count}/{len(self.accounts)} 個專案可用！")
        return success_count

    def __getattr__(self, name):
        srv = self.active_service
        if srv is None:
            raise AttributeError(f"No active YouTube service available for '{name}'")
        return getattr(srv, name)


def get_authenticated_service():
    """獲取與授權 YouTube API v3 Service Pool (支援多專案輪替)"""
    pool = YouTubeServicePool()
    pool.require_expected_accounts()
    pool.require_same_channel()
    if pool.active_service is None:
        logging.error("❌ 無法初始化任何 YouTube API Service！")
        sys.exit(1)
    return pool
