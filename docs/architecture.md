# AUDIO 架構與重構守則（2026-11-25）

## 已完成的去耦與清理

- **來源（`src/sources/`）**：九站維持獨立 Adapter，`registry.py` 是唯一正式來源清單。九站抽測必須從正式 Registry 解析，不能在測試時偷偷補註冊。來源缺文仍報錯。
- **GUI**：`gui_app.py` 是唯一日常入口，`gui_components/` 各自負責章節、批次加入、廣告審核、封面審核、進度監控。大型視窗由獨立 GUI 元件持有。延伸工具分頁可啟動合併上傳與 YouTube 補件的既有程式，不再要求使用者自行尋找多個入口。
- **共用匯入**：GUI 程式使用明確的 `from .runtime import (...)`，`runtime.py` 只使用 `src` 套件匯入；不使用 `sys.path.append` 或異常後默默替換為 `None`。
- **雲端佇列**：`src/queue_dispatcher.py` 依 GitHub Run 真實狀態唯一寫入正式狀態；GUI `queue_monitor.py` 只讀取 Run 並顯示快照，需要結算時觸發 Dispatcher。GUI 不會以過期 Run 覆蓋已重排任務。
- **封面**：`src/metadata_gen.py:create_youtube_cover()` 僅轉呼叫 `src/cover_renderer.py:render_viral_cover()`。已移除失效的歷代封面實作，保留目前輸出規則。
- **Actions**：正式 Workflow 由 20 個整理到 13 個。爬蟲除錯用的短期 Workflow／腳本已刪除；正式九站驗證 `source-live-validation.yml` 保留。主 `audiobook.yml` 的九段 Python 程式移至 `src/ci/audiobook_steps.py`，由可測試的 CLI 入口執行。
- **YouTube OAuth**：只使用已啟用的帳號。保留最多 10 個憑證槽位及未來擴充可能，不動既有 refresh token。

## 穩定契約與禁止事項

1. 不更改 `automation-state` 上現有佇列資料格式、不重設斷點、不刪除歷史 Artifact、不重製既有影片。
2. 製作階段仍是 TXT → 檢查／封面 → 音訊／字幕／影片 → 合併 → HF → YouTube，嚴格依 `success_criteria.py` 與各階段 Checkpoint 驗證。
3. GitHub Dispatcher 是任務狀態唯一正式寫入者。GUI 可提交使用者操作（如新增、暫停、重排），但不得由 Run 監控回寫任務結算。
4. 每站都要採來源自身的章節頁、分頁、字形處理及並行上限；不能將 HTTP 200 當作正文已完整。
5. 重構不得中斷 `main/master` 既有測試或刪除 YouTube 2～10 組未使用槽位。

## 驗證

- 回歸：`xvfb-run -a python -m pytest -q tests src/image/kaggle_chapter_images/tests`。
- 九站實際抓文：`source-live-validation.yml`，需全部來源的首、中、末章成功。
- GUI 視窗：`tests/test_gui_source_ui.py`。
- 架構守則：`tests/test_architecture_boundaries.py`。
- 正式長篇端到端：仍需以真實私有憑證、HF 和 YouTube API 執行；**單元測試與三章抓文無法取代整本書實際製作驗收**。

## 後續技術債（不可誤報已完工）

- `chapter_filter_dialog.py` 仍是大型視窗函式，需再拆為純章節排序/篩選狀態與 UI Widget。
- `src/queue_dispatcher.py` 的調度／還原仍較長，需分成狀態機、GitHub Gateway、Retry Policy，避免破壞既有佇列狀態。
- `src/metadata_gen.py` 仍同時負責小說資料、AI 生成、封面與影片資訊，後續可改為逐步提取可注入服務。
- 合併上傳與 YouTube 補件已可從主 GUI 啟動，但目前仍是**獨立子程序視窗**，不是已完成的共用 Controller／同頁視窗。
- `audiobook.yml` 仍承擔跨 Job 排程與大型 Shell 區塊，應採每一個安全檢查可獨立測試的方式再縮減。


## 舊版單一 Run GUI 已退役

已移除未呈現在介面上的單一 Run 啟動、全域取消、下載及重複監控程式；使用者只能透過實際選取的雲端小說任務進行操作。雲端日誌仍顯示狀態，網路錯誤每 10 秒重試，Run 的正式結算維持於 `dispatch_reconciliation.py`。
