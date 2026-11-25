# 多來源小說架構

來源模組在 `src/sources/`。目前有黃金屋（hjwzw）與 69 書吧
（shuba69）、小說狂人（czbooks）的 HTML 解析器。GUI 目錄解析、正文預覽、CLI 與 Worker
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
3. 在 registry 的 SOURCES 註冊，設定 max_parallel、min_interval；如需 Worker 請求前延遲，在來源模組設定 worker_start_jitter。共用 crawler 不依網站 ID 分支。
4. 加入 HTML fixture 測試：正／倒序、標題、相對 URL、正文與錯誤頁。
5. 解析規則改動時提升 version，重新建立設定，避免沿用舊解析成果。

既有兩站使用已驗證的單頁目錄與正文結構。新增有分頁或 JavaScript 載入的
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

## 小說狂人（czbooks）

書籍網址格式 `https://czbooks.net/n/<book_id>`。來源識別及章節 ID 與舊來源隔離；保留章節 URL query，依站內章節順序整理目錄。預設單 Worker、3 秒間隔。僅供具備相應使用及有聲改編權利的作品使用。2026-11-25 已確認公開書籍頁及章節頁可供瀏覽；執行環境 DNS 無法直接連線，線上 Python 抓取驗證尚未完成。回歸測試使用合成 HTML，不代表線上抓取驗收。

## 2026-11-25 線上驗證

[第一輪九站報告](https://github.com/hub-google/audiobook-generator/actions/runs/45527336329)：黃金屋三章正文成功（3893、5893、4116 個中文字），但目錄第 120 筆實為第 92 回。已修正完整且唯一的 1..N 章號序列排序，版本升為 2。舊設定的章節索引須重新解析確認。69 書吧及小說狂人在 GitHub Runner 收到 403；不可將合成測試通過當作線上成功。請使用有權限的來源或既有使用者提供 HTML 匯入方式。其他六站仍是候選，HTTP 200 不等同完整正文解析。

## 2026-11-25 九站實際驗收與啟用

[線上驗證紀錄](https://github.com/hub-google/audiobook-generator/actions/runs/45527905966)（每站首／中／末三章，不保存或發布正文）：

| 網站 | 實際目錄 | 三章中文字數 | 結果 |
|---|---:|---|---|
| 黃金屋 | 120 | 3893 / 5893 / 5388 | 通過 |
| 小說狂人 | 119 | 4433 / 5880 / 5391 | 通過；先前兩次 403，目錄版本與黃金屋不同 |
| 小說之家 | 544 | 3451 / 3602 / 2624 | 通過，正式註冊 |
| 6K | 9 | 1512 / 1519 / 503 | 通過，正式註冊 |
| 元小說 | 1185 | 2704 / 1971 / 2033 | 通過，正式註冊 |
| 台灣小說網 | 500 | 2491 / 1908 / 111 | 末章過短，暫不註冊 |
| 月亮小說網 | 546 | 首輪三章均分頁未通過 | 已新增分頁載入與文字品質檢查，暫不註冊 |
| 69 書吧 | 0 | — | HTTP 403，保留舊模組及設定相容性 |
| 無限小說 | — | — | 章節指向非小說網域，暫不註冊 |

候選模組不在正式 Registry 中；驗證程序只在自身進程臨時註冊。新來源預設單 Worker、2 秒間隔；分頁由來源專屬 load_catalog/load_chapter 完整合併。若離線 HTML 不完整，明確報錯，不將部分章節誤標為完成。線上抽測不等於全書每章完整或具有改編授權；正式製作僅限公版或已取得相應權利的作品。

## 追加架構診斷（2026-11-25）

- [正常瀏覽器測試](https://github.com/hub-google/audiobook-generator/actions/runs/45528585714)：69 書吧、小說狂人等普通 Chromium 請求得到 403；無限小說同站閱讀頁沒有可讀正文且導航離開原站。
- [原始 HTML 診斷](https://github.com/hub-google/audiobook-generator/actions/runs/45528715425)：台灣小說網目錄每頁 500 筆，確定存在下一頁；已補齊目錄分頁載入。實測第 499、500 章原始正文容器只有 111、100 個中文字，不將缺文視為成功。月亮小說網原始正文含 js-reversed-text 和 icon-* 缺字標記，原始 HTML 並非完整可讀正文；明確拒絕未渲染內容。無限小說目錄連結為不相干外站，同站正文容器僅 8 字；解析器僅接受有效同站目錄及完整可讀正文。
- 69 書吧去重，小說狂人章號連續性與書名標記修正，共用新來源解析保留段落及行內文字。未改變既有 403 處理，也未將候選來源未驗證狀態轉為正式啟用。只處理有相應使用權限的公開內容或授權匯出。

## 台灣小說網獨立驗證

[三部作品測試](https://github.com/hub-google/audiobook-generator/actions/runs/45531007953)：501、648、571 筆完整目錄，均有兩頁；12 筆中九筆正常正文、一筆明確新書公告（24 字）、兩筆來源正文容器只有 100／111 字。解析器已正式註冊（版本 3），明確公告保留原文，不假裝正常故事；短一般章節仍報錯，不自動跳過或用其他作品補文。此為可用但存在來源資料缺漏的站點，不是全站無缺文驗證。

## 授權頁面離線匯入（包含多頁目錄和正文）

`tools/download_novel_txt.py --html-dir <資料夾>` 支援既有單頁檔名格式，也支援 `pages.json`。格式：`{"source_id":"moon17","pages":{"https://www.17moon.com/book/abc/":"catalog.html","https://www.17moon.com/book/abc/def.html":"first.html","https://www.17moon.com/book/abc/def_2.html":"second.html"}}`。URL 必須是實際匯出的同站同書頁面，正文須為正常完整可讀內容。使用原站獨立解析器，沒有網路回退，不跨書補文、不猜測缺字、不自動跳過分頁。沒有匯出齊全會明確失敗。

[獨立線上測試](https://github.com/hub-google/audiobook-generator/actions/runs/45531791840) 顯示無限小說四本書的目錄分別有 47、1104、213、1245 筆，全部章節連向外站；月亮小說網普通 Chromium 的正文請求收到 HTTP 403。兩站僅新增離線合法匯入能力，仍未開放正式線上製作。69 書吧修正 UTF-8／GB18030 解碼及行內文字分段，但線上 403 尚未解除。
