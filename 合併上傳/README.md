# HF 小說全集合併與兩階段上傳

這套工具與小說抓取、封面預檢及一般 Part 發布流程彼此獨立。影片來源是
`HF_ARCHIVE_REPO` 中已完成的約 10 小時 Part MP4，不使用 GitHub Run 的影片 artifacts。

## 啟動 GUI

```powershell
python .\合併上傳\gui.py
```

GUI 會列出具有完整 `part_index.json`、Part MP4 與 `media_info.json` 的小說，並以
FFprobe 保存的秒數顯示真實 MP4 總時長。metadata 會全域平行讀取，結果依 HF repository
建立 15 分鐘本機快取；一般啟動可直接載入，按「重新整理 HF 清單」則強制重新掃描。

合併方式有兩種：

- 每支影片最多指定小時數：依 Part 順序放入最多部數，輸出絕不超過上限。
- 全部合併成一部：所有 HF Parts 形成單一輸出。

GUI 先顯示每支輸出的 Part 範圍、章節範圍、精確時長與容量。確認後才派送
`.github/workflows/merge-hf-book.yml`。

## 雲端流程

1. Actions 以相同 HF revision 重算計畫並比對 GUI 的 `plan_id`。
2. 各輸出由 matrix jobs 使用 FFmpeg stream copy 合併並保存到 HF。
3. 每支影片建立獨立 YouTube resumable session，上傳至 98% 後保存狀態。
4. `.github/workflows/hf-upload-resume-scheduler.yml` 每五分鐘巡檢一次。
5. 暫停滿 24 小時後派送 `.github/workflows/resume-hf-upload.yml`，依 YouTube 回報的
   byte Range 續傳到 100%，再設定 HF 保存的小說封面。

等待 24 小時期間不占用 GitHub runner。Phase 2 最多自動嘗試三次，之後狀態改為
`needs_attention`。

## 必要設定

Repository secrets：`HF_TOKEN`、`YOUTUBE_CLIENT_ID_1`、
`YOUTUBE_CLIENT_SECRET_1`、`YOUTUBE_REFRESH_TOKEN_1`。

Repository variable：`HF_ARCHIVE_REPO`。若未設定，程式使用 HF 登入帳號下的
`audiobook-archive`。
