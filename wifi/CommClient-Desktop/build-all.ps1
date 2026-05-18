#Requires -Version 5.1
<#
.SYNOPSIS
    CommClient — Full Production Build Pipeline (PowerShell)

.DESCRIPTION
    Builds backend server (PyInstaller) + frontend (Vite) + Windows installer (electron-builder + NSIS).
    Output: release\CommClient Setup x.y.z.exe

.PARAMETER SkipServer
    Skip the backend server PyInstaller build step.

.PARAMETER SkipFrontend
    Skip the frontend Vite build step.

.PARAMETER Clean
    Remove previous build artifacts before building.

.EXAMPLE
    .\build-all.ps1
    .\build-all.ps1 -SkipServer
    .\build-all.ps1 -Clean
#>

param(
    [switch]$SkipServer,
    [switch]$SkipFrontend,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = "CommClient [BUILD]"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerRoot  = Join-Path (Split-Path -Parent $ProjectRoot) 'CommClient-Server'
$ReleaseDir  = Join-Path $ProjectRoot 'release'

Set-Location $ProjectRoot

function Write-Step($msg) { Write-Host "`n$('=' * 64)`n  $msg`n$('=' * 64)" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

$sw = [System.Diagnostics.Stopwatch]::StartNew()

# ── Prerequisites ─────────────────────────────────────────

Write-Step "Checking prerequisites"

try { $nodeVer = & node --version 2>&1; Write-Ok "Node.js $nodeVer" }
catch { Write-Fail "Node.js not found" }

try { $pyVer = & python --version 2>&1; Write-Ok "$pyVer" }
catch { Write-Fail "Python not found" }

if (-not $SkipServer) {
    try { $piVer = & pyinstaller --version 2>&1; Write-Ok "PyInstaller $piVer" }
    catch { Write-Fail "PyInstaller not found (pip install pyinstaller)" }

    if (-not (Test-Path $ServerRoot)) { Write-Fail "Server source not found at $ServerRoot" }
}

# ── Clean ─────────────────────────────────────────────────

if ($Clean) {
    Write-Step "Cleaning previous builds"
    foreach ($d in @('dist-electron','release')) {
        $p = Join-Path $ProjectRoot $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Ok "Removed $d" }
    }
    foreach ($d in @('dist','build')) {
        $p = Join-Path $ServerRoot $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Ok "Removed server\$d" }
    }
}

# ── Step 1: Backend Server ────────────────────────────────

if (-not $SkipServer) {
    Write-Step "STEP 1/3: Building Backend Server (PyInstaller)"

    Push-Location $ServerRoot

    Write-Host "  [1a] Installing Python dependencies..."
    & pip install -r requirements.txt --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Fail "pip install failed" }
    Write-Ok "Python dependencies installed"

    Write-Host "  [1b] Running PyInstaller..."
    $specFile = if (Test-Path "CommClient.spec") { "CommClient.spec" } else { "CommClient-Server.spec" }
    & pyinstaller $specFile --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Fail "PyInstaller build failed" }

    $serverExe = Join-Path $ServerRoot 'dist\CommClient-Server\CommClient-Server.exe'
    if (-not (Test-Path $serverExe)) { Pop-Location; Write-Fail "Server exe not found" }
    $sizeMB = [math]::Round((Get-Item $serverExe).Length / 1MB, 1)
    Write-Ok "Server built: CommClient-Server.exe ($sizeMB MB)"

    Pop-Location
} else {
    Write-Host "`n  [SKIP] Backend server build (--SkipServer)" -ForegroundColor DarkGray
}

# ── Step 2: Frontend ──────────────────────────────────────

if (-not $SkipFrontend) {
    Write-Step "STEP 2/3: Building Frontend (Vite + Electron)"

    Write-Host "  [2a] Installing Node dependencies..."
    & npm ci --prefer-offline 2>$null
    if ($LASTEXITCODE -ne 0) { & npm ci }
    if ($LASTEXITCODE -ne 0) { Write-Fail "npm ci failed" }
    Write-Ok "Node dependencies installed"

    Write-Host "  [2b] Type checking..."
    & npx tsc --noEmit 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Warn "TypeScript errors (build continues)" }
    else { Write-Ok "TypeScript check passed" }

    Write-Host "  [2c] Building with Vite..."
    & npx vite build
    if ($LASTEXITCODE -ne 0) { Write-Fail "Vite build failed" }

    $checks = @(
        'dist-electron\main\index.js',
        'dist-electron\preload\index.js',
        'dist-electron\renderer\index.html'
    )
    foreach ($c in $checks) {
        if (-not (Test-Path (Join-Path $ProjectRoot $c))) { Write-Fail "Missing: $c" }
    }
    Write-Ok "Frontend built: main + preload + renderer"
} else {
    Write-Host "`n  [SKIP] Frontend build (--SkipFrontend)" -ForegroundColor DarkGray
}

# ── Step 3: Package Installer ─────────────────────────────

Write-Step "STEP 3/3: Packaging Windows Installer (electron-builder)"

$serverExeCheck = Join-Path $ServerRoot 'dist\CommClient-Server\CommClient-Server.exe'
if (-not (Test-Path $serverExeCheck)) { Write-Fail "Server binary missing. Build server first." }

& npx electron-builder --win --config electron-builder.yml
if ($LASTEXITCODE -ne 0) { Write-Fail "electron-builder failed" }

$installer = Get-ChildItem "$ReleaseDir\*.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'Setup' } | Select-Object -First 1

if (-not $installer) { Write-Fail "Installer exe not found in release\" }

$sizeMB = [math]::Round($installer.Length / 1MB, 1)

$sw.Stop()
$elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 1)

Write-Host ""
Write-Step "BUILD COMPLETE ($elapsed`s)"
Write-Host "  Installer:  $($installer.FullName)" -ForegroundColor Green
Write-Host "  Size:       $sizeMB MB" -ForegroundColor Green
Write-Host ""
Write-Host "  Install (GUI):     `"$($installer.FullName)`""
Write-Host "  Install (Silent):  `"$($installer.FullName)`" /S"
Write-Host "  LAN Deploy:        Copy to \\server\deploy\ and run /S"
Write-Host ""

# Open release folder
Start-Process explorer.exe $ReleaseDir
