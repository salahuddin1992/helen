<#
.SYNOPSIS
    Install Helen-Router AND Helen-Server on the same Windows box,
    automatically wiring them with a shared token.

.DESCRIPTION
    Mirror of bootstrap-router-server.sh.

.EXAMPLE
    PS> Set-ExecutionPolicy -Scope Process Bypass -Force
    PS> .\bootstrap-router-server.ps1 `
            -ServerSetup C:\helen\Helen-Server-Setup-1.0.0.exe `
            -RouterSetup C:\helen\Helen-Router-Setup-1.0.0.exe
#>
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)] [string]$ServerSetup,
    [Parameter(Mandatory)] [string]$RouterSetup
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ServerSetup)) { throw "Server installer not found: $ServerSetup" }
if (-not (Test-Path $RouterSetup)) { throw "Router installer not found: $RouterSetup" }

$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$Token = -join ($bytes | ForEach-Object { $_.ToString("x2") })

Write-Host "[*] Generated shared token" -ForegroundColor Cyan

Write-Host "[1/2] Installing Helen-Router..." -ForegroundColor Cyan
& $RouterSetup /S
Start-Sleep -Seconds 8
$RouterEnv = "$env:ProgramFiles\Helen-Router\.env"
if (Test-Path $RouterEnv) {
    @(
        "HELEN_ROUTER_TOKEN=$Token",
        "HELEN_ROUTER_HOST=0.0.0.0",
        "HELEN_ROUTER_PORT=8080"
    ) | Set-Content -Path $RouterEnv -Encoding ASCII
    Restart-Service HelenRouter -ErrorAction SilentlyContinue
}

Write-Host "[2/2] Installing Helen-Server..." -ForegroundColor Cyan
& $ServerSetup /S
Start-Sleep -Seconds 8
$ServerEnv = "$env:ProgramFiles\Helen-Server\.env"
if (Test-Path $ServerEnv) {
    Add-Content -Path $ServerEnv -Value @(
        "HELEN_REQUIRE_ROUTER=1",
        "HELEN_ROUTER_TOKEN=$Token",
        "HELEN_ROUTER_URL=http://127.0.0.1:8080"
    )
    Restart-Service HelenServer -ErrorAction SilentlyContinue
}

Write-Host
Write-Host "[*] Waiting for services to become healthy..." -ForegroundColor Cyan
$routerOK = 0; $serverOK = 0
for ($i = 0; $i -lt 30; $i++) {
    try { $routerOK = (Invoke-WebRequest -UseBasicParsing http://localhost:8080/router/health -TimeoutSec 2).StatusCode } catch { $routerOK = 0 }
    try { $serverOK = (Invoke-WebRequest -UseBasicParsing http://localhost:3000/api/health -TimeoutSec 2).StatusCode } catch { $serverOK = 0 }
    if ($routerOK -eq 200 -and $serverOK -eq 200) { break }
    Start-Sleep -Seconds 2
}

Write-Host
Write-Host "============================================="  -ForegroundColor Green
Write-Host "  Bootstrap complete"                          -ForegroundColor Green
Write-Host "============================================="  -ForegroundColor Green
Write-Host
Write-Host "  Router health: $routerOK"
Write-Host "  Server health: $serverOK"
Write-Host "  Shared token:  $RouterEnv (and ServerEnv)"
Write-Host
Write-Host "  Verify:"
Write-Host "    curl http://localhost:8080/router/upstreams"
Write-Host "    curl http://localhost:3000/api/auth/login -Method POST  # → 403 (good)"
