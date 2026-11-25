# 多來源小說架構

來源模組在 `src/sources/`。目前有黃金屋（hjwzw）與 69 書吧
（shuba69）的 HTML 解析器。GUI 目錄解析、正文預覽、CLI 與 Worker
使用相同 Registry 和解析器；未知網域不會退回黃金屋。

## 身分與相容性

新 config 的 `source_fingerprint` 是書籍輸入網址的 SHA-256，保留網域、
協定、路徑、尾斜線與 query 的差異，只正規化網域／協定大小寫並去掉 fragment。
同名不同網址是獨立來源；不做跨站合併或自動補章。

新工作目錄為 `Workspace/<source_fingerprint>/`，內部檔名仍保留书名。
HF 書籍根目錄與狀態 key 使用相同指紋。YouTube 播放清單透過 description
中的來源識別比對，同名而不同來源不共用播放清單。未執行任何外部發布。

舊 config 沒有 source_fingerprint 時保留原本以書名命名的目錄、歸檔位置，
供原任務續跑。新任務不自動繼承那些沒有來源證據的檔案，也不搬動舊資料。
書籍 profile schema 升為 4；新版本不再合併尾斜線不同的 URL。
舊版本已經合併過的 URL 無法從歷史資料還原出差異，請重新解析並建立新任務。

## 統一資料

`chapter_records` 包含站內 chapter ID、完整 URL、原始標題、整理後標題
與來源順序；`source_indices` 與 `selected_indices` 保持原本的來源／輸出
編號區分。設定保留目錄快照及解析器版本，續跑沿用該設定。

GUI 新加入的任務會帶上目錄身分摘要，RUN 解析後必須一致才繼續。
目錄變更會要求重新解析選章，不把舊的第 N 筆套到新章節。
舊佇列項目及舊的書籍個別章節設定仍是位置型設定；首次遷移請重新檢查選章。

## 新增網站

1. 建立 SourceAdapter 子類，明列允許的 hostname。
2. 實作 book_id、metadata_url、parse_catalog、parse_chapter；視需要覆寫
   parse_metadata。輸出完整 URL；網站標題處理、廣告移除只放該模組。
3. 在 registry 的 SOURCES 註冊，設定 max_parallel 與 min_interval。
4. 加入 HTML fixture 測試：正／倒序、標題、相對 URL、正文與錯誤頁。
5. 解析規則改動時提升 version，重新建立設定，避免沿用舊解析成果。

當前兩站使用已驗證的單頁目錄與正文結構。新增有分頁或 JavaScript 載入的
網站時，需要在來源層明確實作完整頁面取得，不能只放寬 selector。

## 網路與錯誤

403／401／429 會回報 SourceAccessError；版型改變或正文不存在會回報
SourceParseError。新的爬取流程不以 selector 失敗反覆三次推斷永久缺章。
既有缺章帳本保留供舊任務使用。

HTTP 在單一程序內依來源限速；Actions 按來源限制 Worker 並行數，
黃金屋維持原本 17，69 書吧先用 1。這會連帶限制同一 Worker 的製作吞吐量；
之後如要提高速度，可再將集中抓文與 TTS／影片製作拆成不同 job。
目前沒有跨多個本地程序的全域限流器。

## 下載少量 TXT

```powershell
python tools/download_novel_txt.py https://www.69shuba.com/book/29590/ --count 3 --output-dir Samples
```

成功後產生 `001.txt`～`003.txt` 及帶有 URL、字數、SHA-256 的 manifest。
不會啟動 TTS、GitHub RUN 或上傳。

若一般 HTTP 受限，可自行以瀏覽器儲存 HTML，目錄命名 catalog.html，
章節命名為站內 ID.html，再加上 `--html-dir <資料夾>`。工具仍使用相同
正式來源解析器，不以手工複製文字冒充爬取驗證。

2026-09-05 實測：69 書吧目錄與前三章在瀏覽器可讀；本地 Python 請求
收到 HTTP 403。此次未成功產出三章原文 TXT。TXT 匯出已用合成 fixture
完成整合測試；它不能取代該網站的線上抓取成功驗證。
