@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ==============================================
echo       Audiobook Generator 專案一鍵部署腳本
echo ==============================================
echo.

:: 1. 取得 Commit 訊息
set "MSG=%~1"
if "!MSG!"=="" (
    set /p MSG="請輸入 Commit 訊息 (直接 Enter 則預設為 Auto deploy): "
)
if "!MSG!"=="" set "MSG=Auto deploy"

:: 2. 加入所有變更
echo.
echo [1/3] 正在加入變更至 Git (git add .)
git add .

:: 3. 提交變更
echo.
echo [2/3] 正在提交變更 (git commit)
git commit -m "!MSG!"

:: 4. 從 .env 讀取 GITHUB_TOKEN 並推播
echo.
echo [3/3] 正在推播至 GitHub (git push)...
set "GH_TOKEN="
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="GITHUB_TOKEN" (
            set "GH_TOKEN=%%b"
        )
    )
)

if not "!GH_TOKEN!"=="" (
    rem 移除可能的空白字元
    set "GH_TOKEN=!GH_TOKEN: =!"
    set "GH_TOKEN=!GH_TOKEN:"=!"
    git push https://!GH_TOKEN!@github.com/hub-google/audiobook-generator.git HEAD
    if !errorlevel! equ 0 (
        echo [成功] 已使用 .env 金鑰順利推播！
    ) else (
        echo [錯誤] 推播失敗，請檢查網路或金鑰權限。
    )
) else (
    echo [警告] 找不到 .env 或 GITHUB_TOKEN，將使用預設驗證方式推播...
    git push origin HEAD
)

echo.
echo ==============================================
echo                   部署完成！
echo ==============================================
pause
