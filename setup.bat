@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo FM Face Processor v2.0.0 - セットアップ
echo.

py -3.12 --version >nul 2>&1
if errorlevel 1 (
    echo [エラー] Python 3.12 が見つかりません。
    echo https://www.python.org/downloads/ から Python 3.12 をインストールしてください。
    echo インストール画面では Add python.exe to PATH を有効にしてください。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 専用環境を作成しています...
    py -3.12 -m venv .venv
    if errorlevel 1 goto :failed
)

echo 必要ライブラリをインストールしています...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo セットアップが完了しました。start.bat で起動できます。
pause
exit /b 0

:failed
echo.
echo [エラー] セットアップに失敗しました。通信環境と空き容量を確認してください。
pause
exit /b 1
