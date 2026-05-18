# ============================================================
# install-helen-service.ps1
# Register Helen-Server.exe as a Windows service (auto-start).
# ============================================================
# Requires: Administrator PowerShell.
# Usage:    Right-click → "Run with PowerShell (Admin)"
#           — or —
#           PS> Set-ExecutionPolicy -Scope Process Bypass -Force
#           PS> .\install-helen-service.ps1
#
# The service starts Helen-Server.exe at boot, independent of any
# logged-in user. Helen-Admin.exe (the desktop UI) is *not* needed
# to run — useful for dedicated LAN server PCs in an office.
# ============================================================

$ErrorActionPreference = "Stop"

$ServiceName   = "HelenServer"
$DisplayName   = "Helen Server"
$Description   = "Helen LAN chat backend (FastAPI + Socket.IO, port 3000)."

# ── Resolve the exe path ────────────────────────────────────
# Script lives at <ProjectRoot>\tools\install-helen-service.ps1
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ExePath     = Join-Path $ProjectRoot "dist\Helen-Server\Helen-Server.exe"

if (-not (Test-Path $ExePath)) {
    Write-Host "ERROR: Helen-Server.exe not found at:" -ForegroundColor Red
    Write-Host "  $ExePath" -ForegroundColor Red
    Write-Host ""
    Write-Host "Build the server first:" -ForegroundColor Yellow
    Write-Host "  cd $ProjectRoot"
    Write-Host "  .\venv\Scripts\python.exe -m PyInstaller --noconfirm CommClient-Server.spec"
    exit 1
}

# ── Admin check ────────────────────────────────────────────
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator." -ForegroundColor Red
    Write-Host "Right-click PowerShell and choose 'Run as administrator'."
    exit 1
}

# ── Stop + delete existing service if present ──────────────
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Existing service '$ServiceName' found — stopping and removing..." -ForegroundColor Yellow
    if ($existing.Status -ne "Stopped") {
        try { Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue } catch {}
        Start-Sleep -Seconds 2
    }
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 1
}

# ── Create the service ─────────────────────────────────────
Write-Host "Registering service '$ServiceName' → $ExePath" -ForegroundColor Cyan

# binPath quoting: sc.exe wants the path wrapped in escaped quotes.
$binPath = "`"$ExePath`""

& sc.exe create $ServiceName `
    binPath= $binPath `
    start= auto `
    DisplayName= $DisplayName | Out-Null

if ($LASTEXITCODE -ne 0) {
    Write-Host "sc.exe create failed (exit $LASTEXITCODE)." -ForegroundColor Red
    exit $LASTEXITCODE
}

& sc.exe description $ServiceName $Description | Out-Null

# Restart on failure: 10s delay, up to 3 attempts, then do nothing.
& sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/10000/restart/10000 | Out-Null

# ── Start it ───────────────────────────────────────────────
Write-Host "Starting service..." -ForegroundColor Cyan
Start-Service -Name $ServiceName

Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "Service status: $($svc.Status)" -ForegroundColor Green
Write-Host "Startup type:   $(($svc | Select-Object -ExpandProperty StartType))"
Write-Host ""
Write-Host "Manage with:"
Write-Host "  Start-Service $ServiceName"
Write-Host "  Stop-Service  $ServiceName"
Write-Host "  Get-Service   $ServiceName"
Write-Host "  services.msc  (GUI)"
Write-Host ""
Write-Host "Helen-Server is now running at http://localhost:3000" -ForegroundColor Green
