@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Deploy every non-ignored tracked/untracked workspace change to master.
rem Usage: deploy.bat "Optional commit message"

cd /d "%~dp0"

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] This script is not inside a Git repository.
    exit /b 1
)

for /f "delims=" %%B in ('git branch --show-current') do set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH (
    echo [ERROR] Git is in detached HEAD state. Switch to a branch first.
    exit /b 1
)
if /i not "%CURRENT_BRANCH%"=="master" (
    echo [ERROR] Current branch is %CURRENT_BRANCH%, not master.
    echo [ERROR] Switch to master before running deploy.bat.
    exit /b 1
)

set "DEPLOY_BRANCH=master"

set "DEPLOY_MESSAGE=%~1"
if not defined DEPLOY_MESSAGE set "DEPLOY_MESSAGE=Deploy all workspace changes"

echo [1/4] Staging every added, modified, and deleted file...
git add -A
if errorlevel 1 goto :failed

git diff --cached --quiet
if errorlevel 1 (
    echo [2/4] Creating commit on %DEPLOY_BRANCH%...
    git commit -m "%DEPLOY_MESSAGE%"
    if errorlevel 1 goto :failed
) else (
    echo [2/4] No uncommitted changes; no new commit is needed.
)

echo [3/4] Pushing %DEPLOY_BRANCH% to origin...
git push -u origin "%DEPLOY_BRANCH%"
if errorlevel 1 goto :failed

echo [4/4] Verifying clean deployment state...
for /f "delims=" %%S in ('git status --porcelain') do (
    echo [ERROR] Workspace is still dirty after deployment:
    git status --short
    exit /b 1
)

echo [SUCCESS] All workspace contents were deployed to origin/%DEPLOY_BRANCH%.
exit /b 0

:failed
echo [ERROR] Deployment stopped. Review the Git error above.
exit /b 1
