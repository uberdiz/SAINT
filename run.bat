@echo off
REM Start SAINT. Extra arguments are passed through (e.g. run.bat --background).
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py %*
) else (
    python app.py %*
)
