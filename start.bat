@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo 初回セットアップを開始します。
    call setup.bat
    if errorlevel 1 exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0FM Face Processor.py"
