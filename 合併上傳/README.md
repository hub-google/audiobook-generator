# Run artifacts 合併上傳

這是與原本有聲書產製流程分開的工具。它從指定 GitHub Actions run 下載
`mp4-worker-*` artifacts，根據檔名中的 `chapter_N` 數字升冪排序，無重新編碼合併成
一個 MP4，然後透過 YouTube Data API resumable upload 上傳。

## GUI 操作

1. 開啟 repository 的 **Actions** 頁面。
2. 選擇 **Merge one run into one YouTube video**。
3. 按 **Run workflow**。
4. 填入來源 run ID 與 YouTube 標題。

`checkpoint_repo` 可留空，系統會在 `HF_TOKEN` 所屬帳號建立私有
`audiobook-merge-checkpoints` dataset repo。

工作流只下載 `mp4-worker-*`，不會下載重複的 `video-worker-*`。它不主動切卷，
也不會預先因為影片超過 YouTube 公開限制而中止；最終是否接受由 YouTube API
與處理系統決定。

## 必要 repository secrets

- `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `HF_TOKEN`

## 輸出與狀態

- HF `runs/<run-id>/workers/`：每個 worker 的合併中間檔與排序 manifest。
- HF `runs/<run-id>/merged/merged-audiobook.mp4`：完整合併 checkpoint。
- `merge-upload-state/state.json`：階段、進度、YouTube URL 或失敗細節。
- `merge-upload-state` Actions artifact：不包含巨大 MP4，只保存狀態。

GitHub 只會辨識 `.github/workflows/*.yml`，所以 repository 根目錄會有一個很薄的
`.github/workflows/merge-run-upload.yml`；實作程式與說明都在本資料夾。

## 中斷續做

- 每完成一個 worker 就上傳 HF checkpoint。
- 重跑時略過已完成 worker，不重複下載該來源 artifact。
- 完整合併檔在 YouTube 上傳前先存入 HF。
- YouTube 失敗後重跑會略過 artifact 處理與合併，直接取用合併 checkpoint。
