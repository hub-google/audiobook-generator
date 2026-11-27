# AUDIO 桌面 GUI → GitHub Pages 功能差異與驗收（2026-11-27）

## 基準
原始程式：gui_app.py、gui_components/workflow.py、gui_components/queue_operations.py、gui_components/review.py，核心：src/cloud_queue.py、src/book_profiles.py、src/cleaner.py、src/metadata_gen.py。網頁程式：docs/site 與工具：tools/audio_issue_command.py、GitHub Actions。

**只有每一項前置條件、輸入資料、結果、後製門檻和操作體驗都實測等同桌面 GUI，才能宣稱全面一致。** GitHub Pages 純靜態網站不能在不暴露金鑰的情況下匿名寫入 GitHub。純 GitHub-only 的 Issues→Actions 需要用戶登入 Github 並提交 Issue，操作體驗目前與 GUI 一鍵即時儲存不同。不得虛稱完全一致。

## 功能逐項驗收

| 原 GUI 功能 | 已實際開發的 Pages / Actions 能力 | 目前尚未一致之處 |
| --- | --- | --- |
| 九個來源目錄 | 使用同 Python catalog_parser；69 書吧實測 120 章 | 九站逐站最新實抓驗收 |
| 全書章節篩選、章號、UUID | 全書分頁、UUID/原章號/正規化章號、逐章編輯、多選移動、拖曳重排、Ctrl+方向鍵／空白鍵 | 仍需在真實大型目錄與多種瀏覽器逐項比對桌面快捷鍵 |
| 三種重複條件及直接比對 | 已可選條件、看直接匹配理由、排除重複 | 真實大型目錄同值驗收 |
| 新增、批次、編輯書籍及保存 Profile | 網頁可操作；GitHub Actions 用相同 cloud_queue/book_profiles 核心，條件保存 | 仍須 Github Issues 確認、Profile/Queue 交易失敗回復 |
| 佇列排序、暫停、恢復、刪除、完成、移回及中止 | 單本、多選功能已實作；Issue idempotency、取消等待 GitHub 確認 | 完整多使用者並發與動態狀態機驗收 |
| 製作進度與記錄 | GitHub Run/Job/Step 顯示 | 完整 marker 日誌、即時進度推播 |
| 批次 Gemini 封面資訊 | 已用原 build_cover_information 函式執行於 Actions；網站切換小說、匯入結果、複製目前與全部 Prompt | 真正 Gemini API 配額不足；匯入 Artifact 仍需人工動作 |
| 手動圖片、拖放、裁切、恢復封面 | 已有 JPG/PNG/WEBP、拖放、1280×720、剪貼簿、Issue 上傳、Profile；Cloud 驗證由原 cover_assets 執行 | 必須在 GitHub Issue 附圖再提交，不能直接由純 Pages 私密上傳 |
| Raw/Clean 與廣告候選 | 可匯入真實審核 Artifact、篩選候選及去除/保留、原始逐章與分段；瀏覽器 Clean 和 Python 清理程式跨語言測試 | 全部大型章節與上下文審核的真實同等性 |
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
