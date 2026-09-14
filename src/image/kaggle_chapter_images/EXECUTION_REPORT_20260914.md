# 第 1–3 章 Kaggle → HF 執行報告

## 結果

- Kaggle Kernel：`cit5055/quanzhigaoshou-chapter-images`，version 3 成功。
- Kaggle run ID：`20260914T041558Z`。
- GPU：Tesla P100 16 GB。
- 輸出：第 1–3 章，每章 2 張，共 6 張 1280×720 WebP。
- HF Dataset：`hub-google/audiobook-archive`。
- HF 路徑：`chapter_images/v1/`。
- 發布狀態：`needs_review`（測試候選，尚未人工核准為正式圖片）。
- HF 回讀驗證：6/6 圖片 SHA-256 相符。

## 成功 Kernel 的內部耗時

| 階段 | 耗時 |
|---|---:|
| 相依套件與 P100 相容 PyTorch 安裝 | 190.037 秒 |
| Qwen2.5-7B 文字模型載入 | 75.278 秒 |
| 第 1 章分析 | 81.333 秒 |
| 第 1 章分鏡／Prompt 組合 | 27.439 秒 |
| 第 2 章分析 | 34.613 秒 |
| 第 2 章分鏡／Prompt 組合 | 24.833 秒 |
| 第 3 章分析 | 78.526 秒 |
| 第 3 章分鏡／Prompt 組合 | 26.260 秒 |
| SDXL + Lightning 模型載入 | 92.784 秒 |
| 第 1 章 scene 01 生成 | 5.634 秒 |
| 第 1 章 scene 02 生成 | 4.714 秒 |
| 第 2 章 scene 01 生成 | 4.729 秒 |
| 第 2 章 scene 02 生成 | 4.811 秒 |
| 第 3 章 scene 01 生成 | 4.764 秒 |
| 第 3 章 scene 02 生成 | 4.742 秒 |
| **Kernel 總耗時** | **661.245 秒（11 分 1.245 秒）** |

## 外部傳輸與驗證耗時

| 階段 | 觀測耗時 |
|---|---:|
| 第 1–3 章本機批次準備、編譯與測試 | 1.3 秒 |
| Kaggle 私人輸入 Dataset 初次建立與上傳 | 13.9 秒 |
| Kaggle 輸出下載 | 15.8 秒 |
| GitHub Actions HF 發布工作 | 19 秒 |
| HF 六圖回讀及 SHA-256 驗證 | 21.4 秒 |

## 重試紀錄

| 嘗試 | 結果 | Kaggle 日誌時間 | 原因／修正 |
|---|---|---:|---|
| Kernel v1 | 失敗 | 約 41.6 秒 | Kaggle script bundle 不含旁邊的設定檔；改由私人輸入 Dataset 掛載設定與角色資料。 |
| Kernel v2 | 失敗 | 約 333.3 秒 | 新版 Transformers 回傳 `BatchEncoding`；改為以 `input_ids` 與 `attention_mask` 呼叫 generate。 |
| Kernel v3 | 成功 | 661.245 秒 | 產出 6 張圖片及完整 manifests。 |
| 本機 HF 發布 | 失敗 | 1.558 秒 | 本機 `HF_TOKEN` 是 read-only，無法建立 Dataset。 |
| GitHub Actions HF 發布 | 成功 | 19 秒 | 沿用專案既有 write secret，依內容、manifest、latest 三階段發布。 |

## 品質備註

這批圖片是真正在 Kaggle P100 上生成，未使用其他環境補圖。自動契約與檔案驗證通過，但人工目視發現部分畫面有角色／物件語意偏差，因此依 DOC 規則保持 `needs_review`，沒有冒充 `approved`。後續應加入角色 reference adapter、OCR／圖文一致性與物件錯誤檢查，再針對失敗單圖局部重生。
