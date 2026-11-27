# AUDIO 每小時驗收修復｜第 1 輪（2026-11-28，Asia/Taipei）

## 驗收依據與判定

- 基準：`docs/GUI_WEB_ACCEPTANCE_CRITERIA.md`（A–N，238 項）、`docs/GUI_WEB_PARITY_AUDIT.md`、`gui_components/review.py`、`docs/site/review-inspector.js`、`docs/site/index.html`。
- 本輪完成：逐項比對原桌面 GUI 的 Raw/Clean 與候選詞操作，發現 H21、H23 的實作差異並修改、測試、部署。
- 此輪通過的模擬瀏覽器操作與正式站點靜態/服務驗證 **不等於** 238 項正式全流程 E2E 全數通過。基準文件尚未有通過證據的方格維持未驗收。
- 只對可證明的項目給出部分驗證；不更動正式書籍佇列、不呼叫付費生圖、不對未授權內容進行公開上傳。

## 本輪程式修改

| ID | 發現差異 | 具體修正 | 目前證據與判定 |
| --- | --- | --- | --- |
| H21 管理排除關鍵字 | 網頁 Raw/Clean 缺少桌面 GUI 的「管理排除關鍵字」入口。桌面 GUI 嵌入審核頁時，該按鈕會切換回候選詞／清理規則。 | 新增 `#review-manage-patterns`，切回「候選詞句與排除設定」；沿用已有的去除、保留、新增與確認規則操作。 | **瀏覽器模擬操作 PASS，正式網站部署 PASS；正式書籍儲存 E2E 未測。** |
| H23 Raw/Clean 文字抽查 | 網頁將 Raw/Clean 所有空白都排除後計數，與桌面 Python 的 `len(raw_text.strip())` / `len(clean_text.replace("\n","").strip())` 不同；缺少殘留疑似廣告詞提醒。 | 改用與 Python `len` 相符的 Unicode 字元數、僅扣除 Clean 換行、`max(0, raw-clean)` 清除字數及原五組疑似殘留關鍵字；顯示章名、比例及警示。 | **瀏覽器模擬資料計數 PASS，正式網站部署 PASS；正式 Raw TXT E2E 未測。** |
| N14 部署版本（本輪變更） | 須證明公開站點使用修正後 JS 而非舊版快取。 | 更新 `index.html` 靜態資源版本，並在 Pages 部署完以真實 HTTP 檢查 H21 的按鈕與 H23 的程式碼。 | **正式站點 HTTP 檢查 PASS**；不代表其他 N 類項目都完成。 |

## 真實 GitHub Actions 證據

- 程式及桌面／手機 Chromium 整合驗收：**第 311 次 PASS**，Run [46076708054](https://github.com/hub-google/audiobook-generator/actions/runs/46076708054)。`regression` 與 `ui-smoke` 成功；封面服務測試條件不符被跳過，不算通過。
- 本輪部署及正式網址 HTTP 煙霧測試：**第 130 次 PASS**，Run [46076708025](https://github.com/hub-google/audiobook-generator/actions/runs/46076708025)，含 H21/H23 靜態資產檢查。
- 逐按鈕桌面及手機驗收：**第 80 次 PASS**，Run [46076632765](https://github.com/hub-google/audiobook-generator/actions/runs/46076632765)。
- 瀏覽器截圖可於 Run 46076708054 的 `audio-web-ui-screenshots` 成果及 Run 46076632765 的 `audio-button-audit-screenshots-and-report` 成果中檢查。
- 被驗證的程式 SHA：`35a8ac3ec7f232d6c5ac58cdde12d1bdb99e6252`；修補提交：`117dfc3c`、`4666cd8f`、`0ea98d5a`、`46a93119`、`5e314242`、`aa2e3891`。
- 正式網站：<https://hub-google.github.io/audiobook-generator/>。

## 尚未通過正式 E2E 的重要項目／下一輪優先順序

1. **H01、H22、H28、H29**：有真實 TXT 完成 Artifact 的待審書，實際於 Pages 免登入自動取得候選／Raw/Clean、載入最新書籍 Profile、核對 Raw SHA、重新載入並保存；目前沒有本輪完成的真實待審書可證明。
2. **A09、E07–E22**：以可回滾且不破壞正式內容的方法驗證真正佇列寫入、排序、去重、重送及狀態更新；不能用瀏覽器攔截模擬充當正式成功。
3. **F12、I01–I11、L03–L16**：實際封面及小說從 TXT→封面→核准→Clean→TTS→MP4→HF→YouTube 端到端；受免費配額、外站存取權、授權發布範圍限制；未實測者保留未驗。
4. **其他 A–N 全部未取得本輪正式 E2E 證據的 ID**：維持原本驗收文件的未驗欄位，不虛報已完成。下輪優先按失敗紀錄和風險選項，不重跑已通過的低層級測試。

## 操作原則

每輪保持原 GUI 預設與順序；嚴禁要求使用者下載 ZIP 重上傳、登入 GitHub、手動輸入 Token，嚴禁把 Secrets 放在 Pages/瀏覽器，Supabase 新增資料表一律 `Audio_` 前綴；不可擅自呼叫可能收費的外部服務或向 YouTube 發布非授權測試素材。
