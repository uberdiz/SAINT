@echo off
REM SAINT - Windows setup
REM ======================
REM Creates/repairs .venv, installs all runtime + packaging dependencies,
REM installs the correct PyTorch build for NVIDIA when available, verifies
REM critical imports, checks Ollama, and runs the test suite.

setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo SAINT setup
echo ===========
echo.

echo [1/7] Checking Python...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not on PATH.
    echo Install Python 3.12+ from https://www.python.org/
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: SAINT needs Python 3.12 or newer.
    pause
    exit /b 1
)

echo [2/7] Creating or repairing virtual environment...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: failed to create .venv.
        pause
        exit /b 1
    )
)

set "PY=.venv\Scripts\python.exe"

"%PY%" -m pip install --upgrade pip setuptools wheel
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: failed to update pip/setuptools/wheel.
    pause
    exit /b 1
)

echo [3/7] Installing PyTorch...
nvidia-smi >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo NVIDIA GPU found - installing CUDA 12.8 PyTorch.
    "%PY%" -m pip install --upgrade --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
) else (
    echo No NVIDIA GPU found - installing CPU PyTorch.
    "%PY%" -m pip install --upgrade torch torchaudio
)
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: PyTorch installation failed.
    pause
    exit /b 1
)

echo [4/7] Installing all SAINT dependencies...
"%PY%" -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: dependency installation failed.
    echo The environment was not marked as ready.
    pause
    exit /b 1
)

echo Installing Windows release/build dependencies...
"%PY%" -m pip install --upgrade "PyInstaller>=6.0" "Pillow>=10.0.0"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: packaging dependency installation failed.
    pause
    exit /b 1
)

echo [5/7] Verifying critical imports...
"%PY%" -c "import requests, PySide6, PIL, psutil, numpy, sounddevice, soundfile, onnxruntime, faster_whisper, win32com, uiautomation, keyring, cryptography, zeroconf, segno; print('Core dependencies: OK')"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: one or more required Python packages are missing.
    echo Run setup.bat again to repair the environment.
    pause
    exit /b 1
)

"%PY%" -c "import kokoro_onnx, misaki, en_core_web_sm, espeakng_loader; print('Kokoro voice packages: OK')"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: the Kokoro voice packages are missing - SAINT would fall back to the Windows voice.
    pause
    exit /b 1
)
echo Downloading the Kokoro voice model (about 350 MB, once)...
"%PY%" tools\get_kokoro_onnx.py
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: the Kokoro model download failed. Run: "%PY%" tools\get_kokoro_onnx.py
)

"%PY%" -c "import PyInstaller; print('PyInstaller:', PyInstaller.__version__)"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: PyInstaller is missing.
    pause
    exit /b 1
)

echo [6/7] Checking Ollama...
ollama --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Ollama is not installed.
    echo Install it from https://ollama.com/
    echo Then run: ollama pull llama3.1
) else (
    echo Ollama detected.
)

echo [7/7] Running tests...
"%PY%" -m pytest -q
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: some tests failed - see the output above.
) else (
    echo All tests passed.
)

echo.
echo ============================================================
echo Setup complete.
echo.
echo Start from source:
echo   run.bat
echo.
echo Build the Windows EXE + installer:
echo   "%PY%" packaging\windows\build.py
echo ============================================================
echo.
pause
