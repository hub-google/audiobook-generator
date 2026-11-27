# AUDIO 桌面 GUI → GitHub Pages 功能差異與驗收（2026-11-27）

## 基準
原始程式：gui_app.py、gui_components/workflow.py、gui_components/queue_operations.py、gui_components/review.py，核心：src/cloud_queue.py、src/book_profiles.py、src/cleaner.py、src/metadata_gen.py。網頁程式：docs/site 與工具：tools/audio_issue_command.py、GitHub Actions。

**只有每一項前置條件、輸入資料、結果、後製門檻和操作體驗都實測等同桌面 GUI，才能宣稱全面一致。** GitHub Pages 純靜態網站不能在不暴露金鑰的情況下匿名寫入 GitHub。純 GitHub-only 的 Issues→Actions 需要用戶登入 Github 並提交 Issue，操作體驗目前與 GUI 一鍵即時儲存不同。不得虛稱完全一致。

## 功能逐項驗收

| 原 GUI 功能 | 已實際開發的 Pages / Actions 能力 | 目前尚未一致之處 |
| --- | --- | --- |
| 九個來源目錄 | 使用同 Python catalog_parser；69 書吧實測 120 章 | 2026-11-27 九站已各以第一／中間／最後章實抓 PASS；仍須長篇完整驗證 |
| 全書章節篩選、章號、UUID | 全書分頁、UUID/原章號/正規化章號、逐章編輯、多選移動、拖曳重排、Ctrl+方向鍵／空白鍵 | 仍需在真實大型目錄與多種瀏覽器逐項比對桌面快捷鍵 |
| 三種重複條件及直接比對 | 已可選條件、看直接匹配理由、排除重複 | 真實大型目錄同值驗收 |
| 新增、批次、編輯書籍及保存 Profile | 原 Python Queue/Profile 共用核心；補上可恢復同步紀錄及編輯視覺化選章器、文字清理 Profile 編輯 | 仍須 GitHub Issue 確認；真正多使用者高並發尚未完整驗收 |
| 佇列排序、暫停、恢復、刪除、完成、移回及中止 | 單本、多選功能已實作；Issue idempotency、取消等待 GitHub 確認 | 完整多使用者並發與動態狀態機驗收 |
| 製作進度與記錄 | GitHub Run/Job/Step、每 15 秒唯讀佇列同步、手動刷新工作步驟 | 原 GUI 的完整 Worker marker 即時日誌仍未整合；GitHub 原始 Logs 需要使用者授權 |
| 批次 Gemini 封面資訊 | 已用原 build_cover_information 函式執行於 Actions；網站可自動讀取研究結果、切換小說、匯入備用結果、複製目前與全部 Prompt | 結果已可透過 request_id 自動同步到 Pages，亦保留 Artifact 手動匯入；正式 Gemini API 配額仍限制生產能力 |
| 手動圖片、拖放、裁切、恢復封面 | 已有 JPG/PNG/WEBP、拖放、1280×720、剪貼簿、Issue 上傳、Profile；Cloud 驗證由原 cover_assets 執行 | 必須在 GitHub Issue 附圖再提交，不能直接由純 Pages 私密上傳 |
| Raw/Clean 與廣告候選 | 真實 Artifact 匯入、逐章 Raw/Clean、候選去除／保留／編輯／移除、手動新增詞句，與 Python 清理程式跨語言測試 | 仍須人工匯入 TXT/封面 Artifact；大型小說審核未完成全量驗收 |
| 後製門檻 | Actions 會重新下載對應 TXT Artifact，比對小說 ID 與 Raw SHA256，保存去除與保留詞句，才可以開始後製 | 真正小說完整後製與輸出驗收 |
| 即時／預定發布、台灣時間、策略 A | 網頁可設定，保留既有雲端 YouTube 工作流程 | 真實 YouTube 端對端 |
| 逐項與整包下載 | GitHub 原生 Artifact 下載；單本/多本串流打包成一個 ZIP 並在 Actions 供下載 | 仍須 GitHub 驗證下載與大型資料實際驗收 |
| 無瀏覽器 Token、Secrets 隱藏 | 網頁已移除 PAT 欄位及直接寫入 API，Secrets 只在 GitHub Actions | 不等於不經 GitHub 登入就能安全寫入 |
| AI 封面 HF/Gemini 自動生成 | 已使用原 HF/Gemini fallback 程式與 Actions Secrets | HF 402、Gemini 429 配額限制，未能正式生出封面 |
| 完整目錄→TXT→封面→審核→影片→YouTube | 各個子流程仍在 Repo 中 | 尚沒有當前版本整條正式成功驗收紀錄 |

## 安全與驗收原則
- 不把 GitHub PAT、HF、Gemini、YouTube Secrets 包進 HTML、JS、Pages、下載附件或 DevTools 回應。
- 佇列操作與書籍設定務必使用原 Python 狀態機；Issue 重跑不得重複操作。
- 正式資料不得用 Playwright mock 代替端對端驗收證據。
- 每次修改必須完成 main CI 與桌面／手機 Chromium 測試，必要時截圖並檢查 GitHub Actions。
- 不得以「大部分項目完成」或「單元測試全過」宣稱 100% 同等操作。

## 2026-11-27 補充修正與執行驗證
- 修正解析後預設選取全書，而非前十章；沿用桌面 GUI 的已儲存書籍 Profile 與章節編輯設定。
- 拒絕重複 Issue 覆寫新版 Profile，防止失敗的編輯先污染 Profile；禁止排除全部章節。
- 「只儲存文字清理規則」不會改變待審狀態、不會核准或啟動後製。
- 大型目錄透過磁碟檔案提交 GitHub Contents API，不再將整個 base64 傳入作業系統命令列參數。
- 批次封面資訊已開發自動結果發佈和輪詢，免人工下載匯入（仍需登入 GitHub 送出 Issue）。
- 九個來源的實際章節抽驗：GitHub Run 45922908704，九個獨立 Job 全數通過（非九站全站或全書 100% 保證）。
- CI 第 85 次 Run 45934541601：Python 完整回歸、桌面／手機 Chromium 全數成功；網站部署第 36 次 Run 45934459877 成功。
- GitHub 測試機截圖曾缺繁中字型，已為 UI 驗收工作安裝 Noto Sans CJK；需以後續成功 Run 的截圖覆驗。

## 視覺化編輯補齊
- 既有小說編輯視窗沿用完整目錄選章器，支援逐章勾選／排除、重複判斷、章號與標題、順序調整。目錄透過原本 GitHub Issue→Action 解析，無需在瀏覽器提供 Token。
- 新增小說與編輯小說的目錄、排除清單、章節順序分別管理；關閉視覺化編輯後會恢復新增小說狀態，防止跨書設定污染。
- 此功能仍需 GitHub 送出 Issue；HTML/JS 不含 GH、HF、Gemini 或 YouTube Secret。

## 書籍清理 Profile 一致性
- 編輯既有小說時讀取同一個 GitHub automation-state 書籍 Profile 的最新清理字詞；可編輯、清空或新增，且只有實際修改才寫入，避免舊快照覆蓋新設定。
- 後端將清理詞句加入 Profile 同步交易紀錄（outbox），不冒然把書籍清理設定混進雲端任務欄位；所有變更使用原 Python validate_remove_patterns()。

## 製作進度同步
- 進度對話框增設每 15 秒唯讀同步 GitHub automation-state 公開佇列，更新目前階段、HF / YouTube 統計；視窗關閉即停止查詢。
- 進度視窗有「重新整理進度及工作步驟」，新的 Run 出現時自動更新 Job/Step；避免每 15 秒反覆對 GitHub REST API 請求全部 Jobs，以免吃滿匿名 API 額度。
- 仍非桌面 GUI 的即時 worker log marker：不在 Pages 放 Token，GitHub Job 原始 Logs 需要 GitHub 授權，保留 Run 連結供開啟。

## 2026-11-27 第 97、98 次驗收
- 第 97 次 GitHub Actions 45944324376：後端回歸與桌面／手機 Chromium 驗收成功，涵蓋既有小說完整目錄視覺化編輯。
- 第 98 次 GitHub Actions 45944624153：後端回歸與桌面／手機 Chromium 驗收成功，涵蓋進度視窗唯讀刷新。
- 第 45 次 Pages 部署 45944624162：成功，提交 74fef611474787828a7de92552163e1c3e1f8275。
- 目前尚不能宣稱全 GUI 100% 一致：Issue 送出、原始 TXT/封面 Artifact 下載匯入、手動封面圖片 GitHub 提交、完整 Worker marker、正式全書→YouTube E2E 仍與 GUI 不同或缺乏正式驗收。

## 封面審核完整性修正
- Browser 審核封面新增原始 cover-preflight-manifest.json 核對：artifact_role/schema_version、task_id、master_cover.jpg SHA-256、master_cover_prompt.json SHA-256；不符就拒絕顯示錯誤小說或遭變更的封面。
- TXT 與 Cover 審核資料支援拖放多個 ZIP／JSON，一次讀入、在瀏覽器本機解壓，不向 GitHub Pages 上傳原文或將 Secrets 放入 JavaScript。
- 對於 GitHub 原生 Artifact 仍需 GitHub 授權後下載；純靜態 Pages 不會繞過 GitHub 下載權限。

## 封面實際審核門檻
- 進入第二階段後製前，瀏覽器必須已驗證 TXT Raw 指紋及 Cover Manifest／原圖／Prompt 兩組 SHA-256；僅「儲存清理規則」不用核准封面。
- 後端 Issue Actions 在核准時另以 GitHub 授權下載該書 cover-review Artifact，再次核對 task_id、manifest、圖片與 Prompt SHA-256；不得僅信任瀏覽器提供的指紋。所有驗證失敗仍在 waiting_review，不啟動後製。

## 總覽自動同步
- 參照桌面 GUI 的佇列定期同步行為，Pages 總覽每 45 秒從公開 automation-state JSON 讀取修訂版本，出現較新 revision 才更新列表；使用者回到頁面時也可同步。
- 編輯或審核視窗開啟時暫停總覽重繪，避免異步更新污染編輯表單；讀取失敗保留現有清單，不誤報任務清空，也不需要瀏覽器 Token。

## 審核 ZIP 壓縮炸彈防護
- Browser 匯入審核 ZIP 時，除了壓縮檔本身大小，也檢查中央目錄宣告的解壓大小、總量與資料邊界；超過限制拒絕解壓，避免小 ZIP 解壓成巨量資料。
- UI 驗收另分別截圖：遭修改的封面被拒絕、正常封面被核對；避免把刻意的錯誤測試誤呈現為生產問題。
