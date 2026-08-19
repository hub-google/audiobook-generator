# 08 雲端佇列與 Hugging Face 備份

## 1. 目標

操作者可一次加入多部小說並關閉 GUI；GitHub Actions 必須依固定順位逐部完成。每部小說的 10～11 小時 Part 同時送往 Hugging Face 與 YouTube，任何可重試錯誤均從原 checkpoint 繼續，第一部未嚴格完成前不得啟動第二部。

## 2. 正式佇列

正式狀態位於 `automation-state` 分支的 `audiobook-queue.json`。本地資料只可作快取，不得成為唯一真相來源。每筆任務至少包含 `task_id`、position、book title、catalog URL、選章設定、status、run ID/attempt、HF/YouTube progress、reason、retry_at 與 timestamps。

狀態語意如下：

| 狀態 | 是否阻擋下一部 | 行為 |
|---|:---:|---|
| queued | 否 | 可被調度 |
| dispatching/running | 是 | 不得啟動第二部 |
| waiting_retry | 是 | 到 `retry_at` 前不得呼叫 YouTube；到期 rerun failed jobs |
| needs_attention | 是 | 憑證或永久設定錯誤，等待人工修正 |
| paused/stopped | 否 | 跳過並尋找下一個 queued 任務 |
| canceling | 是 | 等 GitHub Run 真正 cancelled |
| completed | 否 | strict success 已通過 |

更新使用 GitHub Contents API 的 blob SHA 作樂觀鎖；衝突時必須重讀並重套操作。調度 workflow 使用固定 concurrency group，且 dispatch 前將任務原子保留為 `dispatching`。

## 3. 調度器

`.github/workflows/queue-dispatcher.yml` 是短時 Action，由 GUI、`workflow_run: completed` 與每 15 分鐘 schedule 喚醒。每次只讀取、對帳、執行一個決策後結束，不得 sleep 等待小說完成，因此不受單一 Job 六小時限制。每次開始時必須刪除較早且已完成的 Dispatcher Run 紀錄，Actions 清單只保留目前需要判讀的調度紀錄。

調度順序：先處理 blocking task；`waiting_retry` 到期後針對其確定 Run ID 執行 failed-job rerun。沒有 blocking task 時才取 position 最小的 queued task。Production Run 名稱必須包含「有聲小說製作」、小說書名、章節範圍及永久 task ID，以 task ID 配對而不得取「最新 Run」；Dispatcher Run 名稱必須清楚標示「有聲小說佇列調度」與觸發事件。

## 4. 配額與續作

YouTube `quotaExceeded`、`uploadLimitExceeded`、429、字幕／播放清單暫時錯誤必須保存原因與安全 `retry_at`。已完成的 Video ID、字幕、playlist、publish 與 HF Part 不得重作。`waiting_retry` 始終占住目前小說；到期續作第 N Part，完成剩餘 Part 和全書驗證後才釋放下一部。

## 5. HF 目錄契約

```text
有聲小說/<書名>/
  master_cover.jpg
  source_config.yaml
  book_manifest.json
  part_index.json
  有聲小說_<書名>_第01部_第0001章-第0100章/
    <書名>_Part_01_Ch0001_to_Ch0100.mp4
    <書名>_Part_01_Ch0001_to_Ch0100.srt
    part_manifest.json
    youtube_metadata.json
    media_info.json
```

SRT 必須是實際送交 YouTube Caption API 的同一檔案。`master_cover.jpg` 是全書／清單總封面，不是 Part 疊字封面。manifest 記錄章節、來源缺章、task/run、路徑、bytes、SHA-256、YouTube ID 與時間；media info 保存 ffprobe 結果；part index 提供未來長片合併的確定排序。

## 6. 並行與清理閘門

Part 合併與驗證後，用背景工作開始 HF archive，同時由主流程進行 YouTube 上傳。兩邊各自保存 checkpoint。YouTube 完成而 HF 失敗時，重跑只重建／補 HF，不得新增相同 YouTube 影片；HF 完成而 YouTube 失敗時只續作 YouTube。只有兩邊均完成遠端驗證後，才可刪除 Runner 上的 Part MP4 與單章暫存。

## 7. GUI 契約

GUI 主表顯示順位、小說、章節、狀態、HF、YouTube 與 Run ID；支援批量加入、上下移、暫停／恢復、停止、刪除、同步及立即調度。選取小說時，下方紀錄綁定該 task/run。刪除任務預設只刪佇列並取消 active Run，不得刪除已存在的 HF 或 YouTube 成品。

## 8. 完成定義與 Summary

`archive_hf` 是每 Part publication ledger 的必要 step。Actions Summary 必須列出 HF completed/total、repo、逐 Part HF 狀態以及 YouTube 狀態。所有 worker、HF MP4/SRT/總封面/索引、YouTube 影片/字幕/播放清單/公開狀態均完成且回讀成功後，strict gate 才可成功。
