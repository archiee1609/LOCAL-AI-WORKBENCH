# Isotope - AI Companion - PowerShell Stop Script
$ErrorActionPreference = "SilentlyContinue"

Write-Host "[*] Stopping running Isotope, ComfyUI, and Ollama backend processes..." -ForegroundColor Yellow

$procs = Get-Process -Name python, node, ollama -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*app.main*" -or $_.CommandLine -like "*backend\run.py*" -or $_.CommandLine -like "*streamlit*" -or $_.CommandLine -like "*ComfyUI\main.py*" -or $_.ProcessName -eq "ollama"
}

if ($procs) {
    $procs | Stop-Process -Force
    Write-Host "[+] All Isotope, ComfyUI, and Ollama processes stopped successfully." -ForegroundColor Green
} else {
    Write-Host "[i] No active Isotope, ComfyUI, or Ollama processes were found." -ForegroundColor Gray
}
