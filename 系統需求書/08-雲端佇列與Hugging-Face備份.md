# 08 雲端佇列與 Hugging Face 備份

## 1. 目標

操作者可一次加入多部小說並關閉 GUI；GitHub Actions 必須依固定順位逐部完成。每部小說的 10～11 小時 Part 同時送往 Hugging Face 與 YouTube，任何可重試錯誤均從原 checkpoint 繼續，第一部未嚴格完成前不得啟動第二部。

## 2. 正式佇列

正式狀態位於 `automation-state` 分支的 `audiobook-queue.json`。本地資料只可作快取，不得成為唯一真相來源。檔案 schema 現行版本為 2；同一份 JSON 內必須使用兩個彼此獨立的陣列：

- `queue`：只保存尚未嚴格成功完成的任務。每筆任務至少包含 `task_id`、`position`、book title、catalog URL、選章設定、固定 UUID 對應的 `chapter_title_overrides`、status、run ID/attempt、HF/YouTube progress、reason、retry_at 與 timestamps。
- `completed`：只保存已通過 strict success gate 的完成紀錄，保留 task/run、章節、進度及完成時間，但不得包含 `position`，也不得參與調度。

`queue` 與 `completed` 的每筆書籍任務都必須保留穩定 `book_profile_id` 與歷次執行的 `run_history`。同一本書可跨多個 task 保存紀錄；artifact 恢復時以 `book_profile_id` 合併全部歷史，而不是只看目前 task。`run_history` 是候選 Run 的權威索引，書名與 Actions 顯示名稱只供人閱讀。

同一個 `task_id` 不得同時存在於 `queue` 與 `completed`。schema v1 的單一 `tasks` 陣列在讀取時必須依 `status == completed` 分流，移除完成項目的順位，將未完成項目重新連續編號，並在下一次狀態寫入時保存為 schema v2。不得繼續將兩類任務保存在同一個底層陣列後僅由 GUI 篩選。

```json
{
  "schema_version": 2,
  "revision": 414,
  "updated_at": "2026-08-22T10:00:00+00:00",
  "queue": [
    {"task_id": "book-...", "position": 1, "book_title": "吞噬星空", "status": "running"},
    {"task_id": "book-...", "position": 2, "book_title": "完美世界", "status": "queued"}
  ],
  "completed": [
    {"task_id": "book-...", "book_title": "修真聊天群", "status": "completed"}
  ]
}
```

狀態語意如下：

| 狀態 (內部) | GUI 顯示 (無 Run) | GUI 顯示 (有 Run) | 是否阻擋下一部 | 排程器行為 |
|---|---|---|:---:|---|
| queued | idle | queued / pending | 否 | 依順位自動調度 |
| dispatching/running | — | in_progress | 是 | 鎖定順位 1，不得啟動第二部 |
| waiting_retry | — | waiting / retry | 是 | 到 `retry_at` 前不得呼叫 YouTube；到期 rerun failed jobs |
| needs_attention | needs_attention | failed / error | 是 | 憑證或永久設定錯誤，等待人工修正 |
| paused | paused | paused | 否 | 跳過並尋找下一個待命任務 |
| interrupted / stopped | interrupted | interrupted | 否 | 跳過並尋找下一個待命任務；不自動重跑 |
| canceling | — | canceling | 是 | 等 GitHub Run 真正 cancelled 後轉 interrupted |
| completed | completed | completed | 否 | strict success 已通過；只存在於 `completed`，不占排程順位 |

### 順位規則（Position Rules）
1. **順位只屬於未完成任務**：只有 `queue` 內的任務可以有 `position`；`completed` 內所有項目都必須移除該欄位。
2. **順位必須連續**：`queue` 的順位必須恰為 `1..N`，不得缺號、重複或受 `completed` 項目占位影響。新增、移動、刪除、重新排程及成功完成後均須重排。
3. **執行中任務強制鎖定第 1 順位**：任何處於活躍執行狀態（`running`, `dispatching`, `waiting_retry`, `canceling` 或持有未結束之 GitHub Run）的任務，在正規化（`normalize_queue`）與保存（`touch`）時必定自動排在 **順位 1**。
4. **重新排程（Requeue）精確下移**：對任何中斷或需重排的任務點擊「重新排程」時，該任務會自動插入於目前正在執行的活躍任務正下方（**順位 2**），絕不搶佔正在跑的任務。

### 寫入與完成搬移規則

GUI 定時同步、切換頁籤、選取任務或重複取得相同 Run 狀態只可讀取，不得無條件改寫狀態檔。只有新增／移動／刪除、使用者控制、Run ID 綁定、持久狀態改變、進度改變或 schema 遷移等實際資料變更才可更新 `audiobook-queue.json`。

當對應 GitHub Run 的結論為 `success` 且通過完整 strict success gate 時，必須在記憶體中完成以下操作，並以一次 GitHub Contents API 寫入提交整份 JSON：

1. 從 `queue` 移除完成任務。
2. 移除該任務的 `position`，補齊 `completed_at` 及最終進度資料。
3. 依 `task_id` 去重後加入 `completed`。
4. 對剩餘 `queue` 重新編為連續 `1..N`。
5. 寫入失敗時不得留下半套搬移；GitHub blob SHA 衝突時必須重讀並重新套用同一操作。

`failure`、`cancelled`、`interrupted`、`waiting_retry`、`needs_attention` 或 `paused` 都不構成成功完成，不得移入 `completed`。例如原順位 2 的任務成功完成後，原順位 3 必須在同一次更新中成為順位 2。

更新使用 GitHub Contents API 的 blob SHA 作樂觀鎖；衝突時必須重讀並重套操作。調度 workflow 使用固定 concurrency group，且 dispatch 前將任務原子保留為 `dispatching`。

## 3. 調度器

`.github/workflows/queue-dispatcher.yml` 是短時 Action，由 GUI、`workflow_run: completed` 與每 15 分鐘 schedule 喚醒。每次只讀取、對帳、執行一個決策後結束，不得 sleep 等待小說完成，因此不受單一 Job 六小時限制。每次開始時必須刪除較早且已完成的 Dispatcher Run 紀錄，Actions 清單只保留目前需要判讀的調度紀錄。

調度規則：
1. **防併發硬閘門（Concurrency Guard）**：`dispatch_next()` 在發布新任務前，必須先查詢 GitHub Actions；只要 GitHub 上已有任何未結束（`status != 'completed'`）的 `audiobook.yml` Run，調度器一律強制終止調度，絕對不發布第二部小說。
2. **自動對帳活躍 Run**：若 GitHub 上有活躍 Run，調度器自動將對應任務狀態校正為 `running` 並鎖定在順位 1。
3. **取消後自動接續**：當執行中的小說被手動取消（Run Cancelled）並標記為 `interrupted` 後，調度器會直接略過該中斷任務，自動尋找下一筆 `queued` 任務並發布執行。
4. **調度命名**：Production Run 名稱必須包含「有聲小說製作」、小說書名、章節範圍及永久 task ID，以 task ID 配對而不得取「最新 Run」；Dispatcher Run 名稱必須清楚標示「有聲小說佇列調度」與觸發事件。

## 4. 配額與續作

YouTube `quotaExceeded`、`uploadLimitExceeded`、429、字幕／播放清單暫時錯誤必須保存原因與安全 `retry_at`。已完成的 Video ID、字幕、playlist、publish 與 HF Part 不得重作。`waiting_retry` 始終占住目前小說；到期續作第 N Part，完成剩餘 Part 和全書驗證後才釋放下一部。

媒體產製 Run 失敗後的下一次執行，必須從同 `book_profile_id` 的 `run_history` 最新一筆開始逐筆往前檢查。Run 404、Run 尚未產生 worker artifact、artifact 過期／缺失或只含部分有效章節都不得終止回溯；有效部分應累積合併，直到 Worker 完整或歷史耗盡。只有歷史耗盡後仍缺少的內容才可由 Cache 或重新製作補齊。

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

全書 Part plan 鎖定後，由最多 17 個 merge workers 平行合併各自負責的 Part。每個 worker 驗證 MP4/SRT 後必須立即上傳 HF，並以最後寫入的 `merge_manifest.json` 作為原子完成標記；HF 上傳不受 Part 次序限制。大型媒體不得再複製到 GitHub artifact。全部 merge/HF workers 成功後，唯一 YouTube publisher 才能依 Part 編號從 HF 取回、驗證並串行發布。YouTube 失敗時沿用 HF 成品與 Video ID 續傳，不得重做合併或新增相同影片。

## 7. GUI 契約

GUI 使用兩個頁籤，但頁籤必須直接對應不同底層陣列，不得再從單一任務陣列依 status 篩選：

- 「進行中／尚未完成」只讀取 `queue`，顯示連續順位，並支援批量加入、上下移、暫停／恢復、停止、刪除、同步及立即調度。
- 「已完成（Success）」只讀取 `completed`，順位顯示 `—`，不得提供上下移、暫停或重新排程等排程操作。

兩頁均可顯示小說、章節、重複章節、狀態、GitHub 查證時間、HF、YouTube 與 Run ID。

選章對話框另須遵守以下操作契約：

- 第一欄 UUID 為原始目錄固定 1-based 流水號；輸出章號重排不得改動它。
- 章節名稱可雙擊編輯，且修改必須以 UUID 保存並傳入雲端 worker 實際覆蓋標題。
- 最右欄只有「重複」文字具有明細動作；單擊文字必須彈出包含 UUID、網站章節數正規化、網站顯示章節數及章節名稱的完整重複群組。
- Success 頁籤只讀 `completed`；非成功任務不得因 GUI 顯示狀態而被誤分類。
- **狀態顯示契約**：
  - 未向 GitHub 發布 Run 之待命任務，狀態一律顯示為 **`idle`**（非 `queued`），明確區隔未發布與 GitHub Actions Run 排隊。
  - GitHub 真正發布 Run 且等待 Runner 時顯示 **`queued`** / **`pending`**（此時必有 Run ID）。
  - GitHub 執行中顯示 **`in_progress`**。
  - 任務取消或中斷顯示 **`interrupted`**；手動暫停顯示 **`paused`**。
- 選取小說時，下方紀錄綁定該 task/run。刪除任務預設只刪佇列並取消 active Run，不得刪除已存在的 HF 或 YouTube 成品。

## 8. 完成定義與 Summary

`archive_hf` 是每 Part publication ledger 的必要 step。HF media progress 由 merge worker 的 `[HF_MEDIA_MARKER]` 回報；YouTube metadata finalize 後再以 `[HF_ARCHIVE_MARKER]` 確認完整 archive。Actions Summary 必須列出 HF completed/total、repo、逐 Part HF 狀態以及 YouTube 狀態。所有 chapter workers、Part plan、merge/HF workers、HF MP4/SRT/總封面/索引、YouTube 影片/字幕/播放清單/公開狀態均完成且回讀成功後，strict gate 才可成功。
