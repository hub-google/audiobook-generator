# Run artifacts 合併上傳

這是與原本有聲書產製流程分開的工具。最多 15 台 GitHub runner 從指定 run 平行下載
`mp4-worker-*` artifacts，合併各自章節後直接寫入 HF Storage Bucket。全部完成後啟動
HF Job，掛載 Bucket、依章節排序合併成一個 MP4，再 resumable upload 到 YouTube。

## GUI 操作

1. 雙擊 `啟動GUI.bat`。
2. 輸入來源 run ID 與 YouTube 標題。
3. 按「開始雲端合併上傳」。
4. GUI 會顯示 Actions run、目前步驟、成功結果或 failed step log。

GUI 只是雲端控制面板；關閉 GUI 不會取消已送出的 Actions run，
也不會把 MP4 下載到本機。

`checkpoint_repo` 現在代表 Storage Bucket ID；可留空，自動建立公開的
`audiobook-merge-artifacts` Bucket。流程不使用 HF dataset repo，也不產生 HF Git commit。

工作流只下載 `mp4-worker-*`，不會下載重複的 `video-worker-*`。它不主動切卷，
也不會預先因為影片超過 YouTube 公開限制而中止；最終是否接受由 YouTube API
與處理系統決定。

## 必要 repository secrets

- `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `HF_TOKEN`

## 輸出與狀態

- Bucket `runs/<run-id>/workers/`：worker 合併檔與排序 manifest。
- Bucket `runs/<run-id>/merged/merged-audiobook.mp4`：完整合併檔。
- Bucket `runs/<run-id>/state.json`：完成狀態與 YouTube URL。

GitHub 只會辨識 `.github/workflows/*.yml`，所以 repository 根目錄會有一個很薄的
`.github/workflows/merge-run-upload.yml`；實作程式與說明都在本資料夾。

## 中斷續做

- 每個 worker 寫入獨立 Bucket object，不存在 commit 競爭或每小時 commit 限制。
- HF Job 直接掛載 Bucket；合併檔和狀態寫回同一個 run prefix。
