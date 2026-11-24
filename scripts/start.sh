#!/usr/bin/env bash
# ========================================================
#       LOCAL AI WORKBENCH - BACKEND LAUNCH SCRIPT
#       Supports macOS (Apple Silicon & Intel) & Linux
# ========================================================

# Determine directory paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

echo "========================================================"
echo "          LOCAL AI WORKBENCH - LAUNCH SCRIPT            "
echo "========================================================"
echo ""

# Activate virtualenv if present
PYTHON_BIN="python3"
if [ -f "${ROOT_DIR}/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "${ROOT_DIR}/.venv/bin/activate"
    PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
fi

# Set safe environment variables
export OLLAMA_FLASH_ATTENTION=false
export PYTHONPATH="${ROOT_DIR}/backend:${PYTHONPATH}"

# 1. Check & start Ollama service
echo "[*] Checking local Ollama service..."
OLLAMA_RUNNING=0
if curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
    OLLAMA_RUNNING=1
    OLLAMA_VER=$(curl -s http://127.0.0.1:11434/api/version | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
    echo "[+] Ollama is ONLINE! Version: ${OLLAMA_VER}"
else
    echo "[*] Auto-starting local Ollama service in background..."
    OLLAMA_BIN="$(command -v ollama || true)"
    if [ -z "${OLLAMA_BIN}" ]; then
        for cand in /opt/homebrew/bin/ollama /usr/local/bin/ollama /usr/bin/ollama /Applications/Ollama.app/Contents/Resources/ollama "${HOME}/.local/bin/ollama"; do
            if [ -x "${cand}" ]; then
                OLLAMA_BIN="${cand}"
                break
            fi
        done
    fi

    if [ -n "${OLLAMA_BIN}" ]; then
        nohup "${OLLAMA_BIN}" serve >/dev/null 2>&1 &
        sleep 2
    else
        echo "[!] Ollama executable not found. Running in offline/fallback mode."
    fi
fi

# 2. Open browser
URL="http://127.0.0.1:8000"
echo ""
echo "[*] Starting Local AI Workbench server at ${URL}..."
echo "[*] Press Ctrl+C to stop the application."
echo ""

if command -v open >/dev/null 2>&1; then
    # macOS
    (sleep 2 && open "${URL}") &
elif command -v xdg-open >/dev/null 2>&1; then
    # Linux desktop
    (sleep 2 && xdg-open "${URL}") &
fi

"${PYTHON_BIN}" "${ROOT_DIR}/backend/run.py"
