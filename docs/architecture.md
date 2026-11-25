# AUDIO 架構與重構驗收（2026-11-25）

本文件記錄已合併至 master 的程式結構，並區分已清除的主要結構技術債、回歸驗收與外部服務的驗收界線。

## 已完成的主要結構重構

| 功能 | 現在的主要模組與權責 |
|---|---|
| 九站小說 | src/sources 的九站 Adapter、registry.py；正式來源是唯一註冊來源，首中末章以實際正文、字數及 hash 驗證 |
| 雲端結算 | src/dispatch_reconciliation.py 專責任務狀態與 Run 結算；GUI 只讀監控 |
| Actions 派工 | src/dispatch_execution.py 專責預留任務及送出 workflow |
| GitHub API | src/dispatch_github_gateway.py 專責 Run、Jobs、Artifacts、Log 存取 |
| Retry | src/dispatch_retry_policy.py 用純函式分類可重試及永久錯誤 |
| 章節狀態 | src/chapter_selection_state.py 專責排序、勾選、原始章號與輸出序號 |
| 章節 GUI | chapter_table_ui.py、chapter_cell_editors.py、duplicate_review_dialogs.py，各自獨立 |
| 封面考據 | src/cover_research.py，正式書籍 metadata 優先，外部資訊僅供佐證 |
| Gemini | src/cover_ai.py，書籍內容、原著考據及固定模板驗證 |
| HF 圖像 | src/cover_inference.py，包含 SDK/REST、重試與尺寸驗收 |
| 封面輸出 | src/cover_renderer.py 和 src/metadata_gen.py，舊版演算法不再存在 |
| YouTube 回填 | youtube_backfill_backend.py、youtube_studio_client.py、youtube_backfill_gui.py |
| 統一 GUI | gui_app.py 與 tool_launchers.py；合併上傳及 YouTube 工具嵌入同一個 Tk Root，保留獨立啟動 |
| OAuth | youtube_upload 中的已啟用帳號維持原狀，保留日後 1 至 10 組槽位 |
| GitHub Actions | 原 20 個 Workflow 減至 13；主製作 Workflow 的九段內嵌 Python 移至 src/ci/audiobook_steps.py |
| 歷史 GUI | 移除未顯示的單次 Run 啟動、全域取消、下載與重複監控流程 |

## 正式行為契約

1. 不刪除或重置 automation-state、雲端佇列、Checkpoint、HF Artifact、已公開影片或 OAuth 憑證。
2. 主流程 TXT → 封面／前置審核 → 音訊／字幕／影片 → 合併 → HF → YouTube 仍由原驗收條件決定能否完成。
3. GUI 能新增、編輯、取消及重排選取任務；只有 Dispatcher 負責最終 Run 結算，避免舊資料覆蓋新狀態。
4. 九站保留專屬網址、目錄分頁、字形、編碼、正文還原及並行上限；缺文不得視作成功。
5. Gemini 和 HF 既有品質限制不降級，也不使用通用封面或捏造故事內容填補錯誤。
6. 不刪除 YouTube 2 至 10 組尚未啟用的 OAuth 擴充架構。

## 可複製的驗收方式

- 執行 xvfb-run -a python -m pytest -q tests src/image/kaggle_chapter_images/tests，覆蓋 GUI、雲端佇列、封面、YouTube、HF 及架構邊界。
- 執行 source-live-validation.yml 九站真實來源驗證；每站實際目錄和首中末章均要通過。
- 章節視窗使用 test_gui_chapter_editor_smoke.py 作真實 Tk 排序及儲存路徑測試。
- test_final_refactor_boundaries.py 防止已刪除的反射式上傳、舊版全球 Run 控制及重複業務邏輯回流。

## 驗收範圍說明

目前主要結構性補丁和重複責任已整理。完整回歸測試與九站抽測只能驗證對應的受測範圍，不能宣稱所有小說章節都完整，亦不能代替使用私有 OAuth/HF 資料執行完整長篇 YouTube 發布的端到端測試。外站改版、API 配額、Windows DPI 和新需求可能繼續產生技術債；這不代表目前已知的主要架構問題仍未修復。
