@echo off
REM SAINT Revitalized - Windows Setup Script
REM ==========================================
REM This script sets up SAINT on Windows 11

echo.
echo SAINT Revitalized - Windows Setup
echo ===================================
echo.

REM Step 1: Check Python
echo [1/6] Checking Python...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
echo ✓ Python found

REM Step 2: Create virtual environment
echo [2/6] Setting up virtual environment...
if not exist ".venv" (
    python -m venv .venv
    echo ✓ Virtual environment created
) else (
    echo ✓ Virtual environment already exists
)

REM Step 3: Activate and install dependencies
echo [3/6] Installing dependencies...
call .venv\Scripts\activate.bat
pip install --upgrade pip >nul
pip install -r requirements.txt
echo ✓ Dependencies installed

REM Step 4: Check Ollama
echo [4/6] Checking Ollama...
ollama --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Ollama is not installed or not in PATH.
    echo Please install Ollama from https://ollama.com/
    echo SAINT will use Mock provider until Ollama is installed.
) else (
    echo ✓ Ollama found
    echo Pulling model (llama3.2:1b)...
    ollama pull llama3.2:1b
    echo ✓ Model pulled
)

REM Step 5: Initialize data directory
echo [5/6] Initializing data directory...
if not exist "data" mkdir data
if not exist "data\logs" mkdir data\logs
if not exist "data\memory" mkdir data\memory
echo ✓ Data directory initialized

REM Step 6: Run tests
echo [6/6] Running tests...
python -m pytest tests/ -v
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Some tests failed.
) else (
    echo ✓ All tests passed
)

echo.
echo Setup complete!
echo.
echo To start SAINT, run:
echo   python app.py
echo.
echo Or use the run script:
echo   run.bat
echo.
pause