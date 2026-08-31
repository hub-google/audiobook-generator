@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

set "GUI_PYTHON=%CD%\.venv\Scripts\pythonw.exe"
if not exist "%GUI_PYTHON%" (
  echo.
  echo [ERROR] 找不到專案虛擬環境：.venv
  pause
  exit /b 1
)

start "" "%GUI_PYTHON%" "%CD%\tools\youtube_backfill_gui.py"
exit /b 0
