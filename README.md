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
