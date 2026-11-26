@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo        ISOTOPE - AI COMPANION LAUNCH SCRIPT
echo ========================================================
echo.

set "ROOT_DIR=%~dp0.."
cd /d "%ROOT_DIR%"

:: Set CUDA safety flags for Ollama
set OLLAMA_FLASH_ATTENTION=false

:: 1. Check & start local Ollama service
echo [*] Checking local Ollama service...
python -c "import urllib.request, json; v = json.loads(urllib.request.urlopen('http://127.0.0.1:11434/api/version', timeout=2).read().decode())['version']; print(f'[+] Ollama is ONLINE - Version: {v}')" 2>nul
if errorlevel 1 (
    echo [*] Auto-starting local Ollama service...
    start "Ollama" /min ollama serve
    ping 127.0.0.1 -n 4 >nul
)

:: 2. Launch Backend & Open Browser
echo.
echo [*] Starting Isotope server at http://127.0.0.1:8000...
echo [*] Press Ctrl+C in this terminal window to stop the application.
echo.

start "" "http://127.0.0.1:8000"
python backend\run.py
