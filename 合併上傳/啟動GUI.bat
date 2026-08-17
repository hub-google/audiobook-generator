@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHON_EXE=C:\DevTools\Python312\python.exe"
if exist "%PYTHON_EXE%" goto run
set "PYTHON_EXE=python"
:run
"%PYTHON_EXE%" "合併上傳\gui.py"
if errorlevel 1 pause
endlocal
