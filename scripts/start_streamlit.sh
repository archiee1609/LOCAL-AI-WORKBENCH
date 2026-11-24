#!/usr/bin/env bash
# ========================================================
#       LOCAL AI WORKBENCH - STREAMLIT LAUNCHER
#       Supports macOS (Apple Silicon & Intel) & Linux
# ========================================================

# Determine directory paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

echo "========================================================"
echo "        LOCAL AI WORKBENCH - STREAMLIT LAUNCHER         "
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
if curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
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

# 2. Launch Streamlit Application
URL="http://127.0.0.1:8501"

if curl -s -m 1 http://127.0.0.1:8501/_stcore/health >/dev/null 2>&1 || curl -s -m 1 http://127.0.0.1:8501 >/dev/null 2>&1; then
    echo "[+] Local AI Workbench is already running at ${URL}!"
    echo "[*] Opening in browser..."
    if command -v open >/dev/null 2>&1; then
        open "${URL}"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "${URL}"
    fi
    exit 0
fi

echo ""
echo "[*] Starting Local AI Workbench via Streamlit at ${URL}..."
echo "[*] Press Ctrl+C to stop the application."
echo ""

if command -v open >/dev/null 2>&1; then
    (sleep 2 && open "${URL}") &
elif command -v xdg-open >/dev/null 2>&1; then
    (sleep 2 && xdg-open "${URL}") &
fi

"${PYTHON_BIN}" -m streamlit run "${ROOT_DIR}/streamlit_app.py" --server.port 8501 --server.headless true
