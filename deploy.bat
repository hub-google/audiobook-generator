@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

cd /d "%~dp0"

echo =======================================================
echo          [音頻小說] 開始執行專案部署 (deploy.bat)
echo =======================================================
echo.

rem 1. 自動修復 Google Drive 造成的 .git (1) 同步衝突資料夾
if not exist ".git" if exist ".git (1)" (
    echo [INFO] 偵測到 Google Drive 同步衝突資料夾 .git ^(1^)，正在修復重命名為 .git ...
    ren ".git (1)" ".git"
)

rem 2. 清理可能殘留的鎖定檔案
if exist ".git\packed-refs.lock" del /f /q ".git\packed-refs.lock" >nul 2>&1
if exist ".git\index.lock" del /f /q ".git\index.lock" >nul 2>&1

rem 3. 檢查 Git 是否可用
where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 系統找不到 Git 指令，請確認已安裝 Git 且已加入 PATH。
    goto :failed
)

rem 4. 檢查是否在 Git 儲存庫內
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 當前目錄並非 Git 儲存庫（找不到有效的 .git 目錄）。
    goto :failed
)

rem 5. 檢查當前分支
set "CURRENT_BRANCH="
for /f "delims=" %%B in ('git branch --show-current') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
    echo [ERROR] Git 目前處於 detached HEAD 狀態，請先切換至分支。
    goto :failed
)
if /i not "%CURRENT_BRANCH%"=="master" (
    echo [ERROR] 當前分支為 [%CURRENT_BRANCH%]，並非 master 分支。
    echo [ERROR] 請先切換至 master 分支後再執行 deploy.bat。
    goto :failed
)

set "DEPLOY_BRANCH=master"

set "DEPLOY_MESSAGE=%~1"
if not defined DEPLOY_MESSAGE set "DEPLOY_MESSAGE=Deploy all workspace changes"

echo [1/5] 正在暫存變更檔案: git add -A
git add -A
if errorlevel 1 goto :failed

git diff --cached --quiet
if errorlevel 1 (
    echo [2/5] 建立 Commit: "%DEPLOY_MESSAGE%"
    git commit -m "%DEPLOY_MESSAGE%"
    if errorlevel 1 goto :failed
) else (
    echo [2/5] 沒有未提交的變更，略過 Commit 步驟。
)

echo [3/5] 正在從遠端同步最新變更: git pull --rebase origin %DEPLOY_BRANCH%
git pull --rebase origin "%DEPLOY_BRANCH%"
if errorlevel 1 goto :failed

echo [4/5] 正在推送至 GitHub: git push -u origin %DEPLOY_BRANCH%
git push -u origin "%DEPLOY_BRANCH%"
if errorlevel 1 goto :failed

echo [5/5] 正在驗證工作區狀態...
for /f "delims=" %%S in ('git status --porcelain') do (
    echo [ERROR] 部署後工作區仍有未乾淨的檔案:
    git status --short
    goto :failed
)

echo.
echo =======================================================
echo [SUCCESS] 部署成功！所有工作區變更已推送至 origin/%DEPLOY_BRANCH%。
echo =======================================================
echo.
pause
exit /b 0

:failed
echo.
echo =======================================================
echo [ERROR] 部署未完成，請檢查上方 Git 錯誤訊息。
echo =======================================================
echo.
pause
exit /b 1