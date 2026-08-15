@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 先に setup.bat を実行してください。
    pause
    exit /b 1
)

echo EXE作成用ライブラリを準備しています...
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :failed

echo FM Face Processor.exe を作成しています...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed ^
  --name "FM Face Processor" ^
  --collect-submodules rembg.sessions ^
  --collect-data rembg ^
  --collect-all rapidocr_onnxruntime ^
  --collect-all sv_ttk ^
  --collect-all tkinterdnd2 ^
  --add-data "face_detection_yunet_2023mar.onnx;." ^
  --add-data "Real-ESRGAN-x4plus.onnx;." ^
  --add-data "real_esrgan_x4plus.data;." ^
  "FM Face Processor.py"
if errorlevel 1 goto :failed

echo.
echo 完了: dist\FM Face Processor\FM Face Processor.exe
pause
exit /b 0

:failed
echo.
echo [エラー] EXEの作成に失敗しました。
pause
exit /b 1
