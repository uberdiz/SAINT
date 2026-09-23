@echo off
REM SAINT - Windows setup
REM ======================
REM Creates .venv, installs dependencies (CUDA PyTorch when an NVIDIA GPU is
REM present), pulls a tool-capable Ollama model and runs the test-suite.

setlocal
echo.
echo SAINT setup
echo ===========
echo.

echo [1/6] Checking Python...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not on PATH. Install Python 3.12+ from https://www.python.org/
    pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: SAINT needs Python 3.12 or newer.
    pause
    exit /b 1
)

echo [2/6] Creating virtual environment...
if not exist ".venv" python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul

echo [3/6] Installing PyTorch...
nvidia-smi >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo NVIDIA GPU found - installing CUDA 12.8 PyTorch ^(required for RTX 50-series^)
    pip install --index-url https://download.pytorch.org/whl/cu128 torch torchaudio
) else (
    echo No NVIDIA GPU found - installing CPU PyTorch ^(speech output will run on the CPU^)
    pip install torch torchaudio
)

echo [4/6] Installing SAINT dependencies...
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: dependency installation failed.
    pause
    exit /b 1
)

echo [5/6] Checking Ollama...
ollama --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Ollama is not installed. Install it from https://ollama.com/ and run:
    echo     ollama pull llama3.1
) else (
    echo Pulling llama3.1 ^(supports tool calling^)...
    ollama pull llama3.1
)

echo [6/6] Running tests...
python -m pytest -q
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: some tests failed - see the output above.
) else (
    echo All tests passed.
)

echo.
echo Setup complete. Start SAINT with:  run.bat   ^(or: .venv\Scripts\python app.py^)
echo.
pause
