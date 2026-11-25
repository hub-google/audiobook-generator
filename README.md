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
