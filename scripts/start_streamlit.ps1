# Isotope - AI Companion - Streamlit PowerShell Launch Script
$ErrorActionPreference = "Continue"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "     ISOTOPE - AI COMPANION STREAMLIT LAUNCHER" -ForegroundColor Cyan
Write-Host "========================================================`n" -ForegroundColor Cyan

$rootDir = Split-Path -Parent $PSScriptRoot
Set-Location $rootDir

# Set CUDA safety flags for Ollama
$env:OLLAMA_FLASH_ATTENTION = "false"

# 1. Check & start local Ollama service
try {
    $ollama = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 2 -ErrorAction Stop
    Write-Host "[+] Ollama server is ONLINE (Version: $($ollama.version))" -ForegroundColor Green
} catch {
    Write-Host "[*] Auto-starting local Ollama service..." -ForegroundColor Green
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}


Write-Host "`n[*] Starting Isotope via Streamlit at http://127.0.0.1:8501..." -ForegroundColor Green
Write-Host "[*] Opening browser..." -ForegroundColor Gray
Start-Process "http://127.0.0.1:8501"

python -m streamlit run streamlit_app.py --server.port 8501 --server.headless true
