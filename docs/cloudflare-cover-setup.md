# Cloudflare Workers AI 封面設定

正式封面圖片生成使用 `@cf/black-forest-labs/flux-1-schnell`。Cloudflare 未設定、限流、額度不足或失敗時直接停止，不自動呼叫其他付費生圖 API。Gemini 文字分析、HF 儲存、手動封面與其他影音階段不變。

一次性帳號設定：

1. Cloudflare 控制台確認 Workers 方案為 Free（非 Paid）。
2. 建立僅限該帳號、具有 Workers AI 執行推論權限的 API Token。
3. GitHub Settings → Secrets and variables → Actions → Secrets，新增 `CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_API_TOKEN`。
4. Variables 新增 `CLOUDFLARE_WORKERS_FREE_CONFIRMED`，值為 `true`。這是人工確認，不會自動查核方案；若未來升級 Paid，請先移除該變數以停用生圖。
5. Actions → 系統測試｜程式、網站與封面 → Run workflow；下載 cloudflare-cover-probe artifact 驗證實際 1280×720 圖片。

不可將 Token 放到網頁、config.yaml、公開變數或 Git。桌面 GUI 若直接在本機生成封面，需在本機執行環境提供同名設定。模型目前沒有畫布尺寸輸入，回傳圖等比裁切成 1280×720。

官方：[模型](https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/)｜[價格](https://developers.cloudflare.com/workers-ai/platform/pricing/)。
