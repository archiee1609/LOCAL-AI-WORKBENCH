@echo off
setlocal enabledelayedexpansion

echo ========================================================
echo        LOCAL AI WORKBENCH - STREAMLIT LAUNCHER
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

:: 2. Launch ComfyUI service if present
if exist "%ROOT_DIR%\ComfyUI\main.py" (
    echo [*] Checking ComfyUI service...
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=1); print('[+] ComfyUI is already ONLINE')" 2>nul
    if errorlevel 1 (
        echo [*] Auto-starting local ComfyUI alongside Ollama...
        start "ComfyUI Server" /min cmd /c "cd /d ""%ROOT_DIR%\ComfyUI"" && python main.py --listen 127.0.0.1 --port 8188 --cpu --use-split-cross-attention"
        ping 127.0.0.1 -n 4 >nul
    )
)

:: 3. Launch Streamlit Application
echo.
echo [*] Starting Local AI Workbench via Streamlit at http://127.0.0.1:8501...
echo [*] Press Ctrl+C in this terminal window to stop the application.
echo.

python -m streamlit run streamlit_app.py --server.port 8501 --server.headless true
