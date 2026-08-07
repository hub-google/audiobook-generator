@echo off
setlocal EnableDelayedExpansion

echo ==============================================
echo    Audiobook Generator Auto-Deploy Script
echo ==============================================
echo.

:: 1. Get commit message
set "MSG=%~1"
if "!MSG!"=="" (
    set /p MSG="Enter commit message (Press Enter for 'Auto deploy'): "
)
if "!MSG!"=="" set "MSG=Auto deploy"

:: 2. Add changes
echo.
echo [1/3] Adding changes to Git (git add .)
git add .

:: 3. Commit changes
echo.
echo [2/3] Committing changes (git commit)
git commit -m "!MSG!"

:: 4. Read GITHUB_TOKEN from .env and push
echo.
echo [3/3] Pushing to GitHub (git push)...
set "GH_TOKEN="
if exist .env (
    for /f "tokens=1,2 delims==" %%a in (.env) do (
        if "%%a"=="GITHUB_TOKEN" (
            set "GH_TOKEN=%%b"
        )
    )
)

if not "!GH_TOKEN!"=="" (
    rem Remove possible whitespace and quotes
    set "GH_TOKEN=!GH_TOKEN: =!"
    set "GH_TOKEN=!GH_TOKEN:"=!"
    git push https://!GH_TOKEN!@github.com/hub-google/audiobook-generator.git HEAD
    if !errorlevel! equ 0 (
        echo [SUCCESS] Pushed successfully using token from .env!
    ) else (
        echo [ERROR] Push failed. Please check network or token permissions.
    )
) else (
    echo [WARNING] .env or GITHUB_TOKEN not found. Falling back to default auth...
    git push origin HEAD
)

echo.
echo ==============================================
echo              Deployment Complete!
echo ==============================================
pause
