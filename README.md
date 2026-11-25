# 有聲小說製作系統

本機 GUI（`python gui_app.py`）與 GitHub Actions 使用同一雲端佇列，狀態資料保存在 `automation-state`，正式影音由既有 HF archive 保存。

- TXT 與封面各自執行，人工審核後才啟動後製。
- 前置作業原生失敗重跑最多 20 次；保留成功 Job。
- Worker 保存一份完整 `video-worker-N` 檔案包及輕量 manifest；只在失敗時保存 Workspace Cache。歷史 `mp4-worker-N` 仍可讀取。
- 插圖生成屬獨立 Kaggle 流程，圖片必須通過 manifest 驗證並保留 needs_review。
- 單元與回歸測試：`python -m pytest -q tests src/image/kaggle_chapter_images/tests`。Linux GUI 測試用 xvfb-run。

## 資料保留

重構前的原始檔案仍由 `archive/pre-refactor-data-20261125` 分支保留（commit `d2b5a314a24e3637eb3640a9ff009efcfdb0c286`），不移除 Git 歷史，也不刪除 HF 或 YouTube 既有內容。
小說文字、暫存佇列、Kaggle 批次資料及本機產物不再納入日常程式碼版本。
僅使用有合法授權的本機小說來源，以 `prepare_batch.py --source <資料夾>` 重新生成批次。若需復原舊資料，使用 archive 分支精確取回所需目錄。

## 外部服務驗收與 YouTube 授權修復

在本機專案目錄中執行 `python src/youtube_api_uploader.py --auth-pool`。程式使用既有 `client_secret/client_secret_N.json` 或本機設定，對失效授權開啟瀏覽器，並在登入 GitHub CLI 後將有效憑證同步至本專案的 GitHub Secrets。同步失敗不會顯示為成功；不需在聊天中提供任何 Token。未取得 Google 使用者授權前，不得宣稱該憑證已修復。

正式 Worker 的 read-only 驗證使用原創文字進行短影音製作，不發布到 YouTube、不變更 HF 既有作品。1,700 章測試驗證分工、排除章節與分部清單，不代表已實際生成 1,700 章語音。

目前正式 Actions 預設只啟用第 1 組 YouTube 憑證；原有第 2～10 組結構與 Secrets 均保留。日後新增授權後，在 GitHub Actions repository variables 設定 `YOUTUBE_ACTIVE_SLOTS`（例如 `1,2,3`）即可啟用指定組別。新 HF 合併影片只會分配到已啟用組別，既有上傳 session 仍綁定原 OAuth 組別，不會任意切換。

## GUI 支援的小說來源（9 站）

執行 `python gui_app.py`，在「新增小說／章節設定」選擇網站或直接貼上網址；程式會依已註冊的站點解析器自動辨識網站，並將書籍頁、章節頁、分頁目錄轉為適合的**完整目錄網址**。GUI 會顯示來源網站、目錄章數、重複章節與各站並行上限。來源欄位變更後須重新解析，避免把舊書的章節送入新網址。

支援黃金屋（hjwzw）、69 書吧（shuba69）、小說狂人（czbooks）、台灣小說網（twkan）、6K 小說網（sixk）、小說之家（xszj）、元小說（metanovel）、月亮小說網（moon17）、無限小說（book8）；GUI 清單直接對應 `src/sources/registry.py` 註冊的九個模組，新增來源時不應只修改 GUI 選項。每站均提供已驗證示例網址，可從網站選單快速帶入。連線存取受限、目錄缺頁、正文缺漏會報錯，不會假裝完整。

「批量加入網址」的手動頁面支援九站混合 URL，按原輸入順序處理，依本站書籍識別碼和查詢條件排除同書重複；69 書吧完本排行榜仍屬該站專用功能，並非全網站通用排行榜。雲端工作流從目錄解析結果自動取得 `source_id`、`parser_version` 及 `source_max_parallel`；各站的目錄分頁、文字還原、AJAX 正文和重試政策仍由個別爬蟲負責。可用 `pytest -q tests/test_gui_source_ui.py` 檢驗九站 GUI URL 流程。

GUI 的「試抓首章」按鈕使用和正式 Worker 完全相同的來源解析器，非同步取得首章並顯示完整文字與中文字數；不會寫入佇列。當來源網址修改、尚未解析或目錄失效時，會停用「試抓首章／加入佇列」，必須重新解析以避免錯書。

雲端佇列現在也顯示每本小說的「來源」欄，混合批次可直接辨識各任務是哪個網站，既有雲端任務以其保存的來源網址自動辨識，不需要遷移佇列資料。
