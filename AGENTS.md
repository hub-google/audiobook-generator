# Workspace tool paths

- GitHub CLI is installed at `C:\Program Files\GitHub CLI\gh.exe`.
- `gh.exe` is not necessarily present on `PATH`; always invoke it using the absolute path above.

# Workflow boundaries

- Cover preflight and novel scraping must remain independent workflows.
- `.github/workflows/cover-preflight.yml` must not run `catalog_parser.py`, `crawler.py`, or fetch a novel source website.
- In cover preflight, `catalog_url` may only be used to validate book identity and derive stable storage keys. It must not trigger an HTTP request.
- Cover preflight must build its minimal configuration from dispatched inputs and the persisted book-profile snapshot. It must not require chapter lists, chapter titles, selection matrices, or scraper artifacts.
- Before changing a workflow, identify its required inputs, produced artifacts, and downstream consumers. Do not invoke a larger workflow merely to obtain a small subset of its data.
- When changing cover, scraping, merge, or publication behavior, run tests for both the changed stage and its directly adjacent stages.
- If the existing tests do not enforce a workflow boundary affected by a change, add a regression test before considering the change complete.

# TXT Scraper 失敗重跑規則

- TXT Scraper 失敗後，必須使用 GitHub Actions 原生的 `rerun-failed-jobs` 重跑；不得把同一個 runner 內重試 HTTP 請求或重建 Session 當成 Re-run，因為那不會更換出口 IP。
- GitHub 原生 Re-run 只能在當輪 workflow attempt 完成後啟動；當輪全部 scraper jobs 結束後，必須立即重跑失敗的 jobs，不得套用一般的兩小時等待規則，也不得等待 15 分鐘排程。
- 每輪只重跑失敗的 scraper jobs；已成功的 scraper jobs 及其 artifacts 必須保留，不得重跑。
- 同一個 TXT 抓取 Run 最多自動 Re-run 20 次；第 20 次 Re-run 仍失敗後，才能停止並將任務標記為 `needs_attention`。
- 這個 20 次上限指的是 GitHub 原生 Re-run 次數，不包含最初的 workflow attempt；因此最多為 1 次初始執行加 20 次 Re-run。
- 修改 scraper 重跑行為時，必須加入或更新回歸測試，至少覆蓋：當輪完成後立即 Re-run、只重跑失敗 jobs，以及 20 次上限。
