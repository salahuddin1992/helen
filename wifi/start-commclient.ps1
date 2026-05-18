# ============================================
#  CommClient — Master Launcher (PowerShell)
#  Starts both Server and Desktop in parallel
# ============================================

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "===================================" -ForegroundColor Cyan
Write-Host " CommClient — LAN Communication"     -ForegroundColor Cyan
Write-Host "===================================" -ForegroundColor Cyan
Write-Host ""

# ── Start Server ─────────────────────────────
Write-Host "[1/2] Launching Server..." -ForegroundColor Green
$serverProc = Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$Root\CommClient-Server'; & '.\scripts\start-server.bat'"
) -PassThru

# Wait for server initialization
Write-Host "[INFO] Waiting 5s for server..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

# ── Start Desktop ────────────────────────────
Write-Host "[2/2] Launching Desktop..." -ForegroundColor Green
$desktopProc = Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$Root\CommClient-Desktop'; & '.\scripts\start-desktop.bat'"
) -PassThru

Write-Host ""
Write-Host "===================================" -ForegroundColor Cyan
Write-Host " Both processes launched."           -ForegroundColor Cyan
Write-Host " Server PID:  $($serverProc.Id)"    -ForegroundColor White
Write-Host " Desktop PID: $($desktopProc.Id)"   -ForegroundColor White
Write-Host " Server:  http://localhost:3000"     -ForegroundColor White
Write-Host "===================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Enter to stop both processes..." -ForegroundColor Yellow
Read-Host

# Cleanup
Write-Host "Stopping processes..." -ForegroundColor Red
Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $desktopProc.Id -Force -ErrorAction SilentlyContinue
Write-Host "Done." -ForegroundColor Green
