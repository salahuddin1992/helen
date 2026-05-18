# ============================================================
# uninstall-helen-service.ps1
# Remove the HelenServer Windows service.
# ============================================================
# Requires: Administrator PowerShell.
# ============================================================

$ErrorActionPreference = "Stop"
$ServiceName = "HelenServer"

# Admin check
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator." -ForegroundColor Red
    exit 1
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Service '$ServiceName' is not installed. Nothing to do." -ForegroundColor Yellow
    exit 0
}

if ($existing.Status -ne "Stopped") {
    Write-Host "Stopping service..." -ForegroundColor Cyan
    try { Stop-Service -Name $ServiceName -Force } catch {}
    Start-Sleep -Seconds 2
}

Write-Host "Removing service..." -ForegroundColor Cyan
& sc.exe delete $ServiceName | Out-Null

if ($LASTEXITCODE -eq 0) {
    Write-Host "Service '$ServiceName' removed." -ForegroundColor Green
} else {
    Write-Host "sc.exe delete returned exit $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
