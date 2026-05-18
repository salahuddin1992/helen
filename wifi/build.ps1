#Requires -Version 5.1
<#
.SYNOPSIS
    CommClient — Master Build Pipeline (PowerShell)

.DESCRIPTION
    Builds the full CommClient platform:
      Stage 1: Python backend → PyInstaller single-folder exe
      Stage 2: React frontend → Vite + Electron main/preload
      Stage 3: NSIS installer via electron-builder

.PARAMETER Target
    Build target: 'all' (default), 'server', 'desktop', 'installer'

.PARAMETER SkipDeps
    Skip dependency installation (npm install / pip install)

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Target server
    .\build.ps1 -Target installer -SkipDeps
#>

param(
    [ValidateSet('all', 'server', 'desktop', 'installer')]
    [string]$Target = 'all',

    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerDir = Join-Path $Root 'CommClient-Server'
$DesktopDir = Join-Path $Root 'CommClient-Desktop'
$ServerDist = Join-Path $ServerDir 'dist\CommClient-Server'
$ReleaseDir = Join-Path $DesktopDir 'release'
$Errors = 0
$StartTime = Get-Date

Write-Host ''
Write-Host '  ================================================' -ForegroundColor Cyan
Write-Host '   CommClient — Master Build Pipeline (PowerShell)' -ForegroundColor Cyan
Write-Host "   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
Write-Host "   Target: $Target" -ForegroundColor Gray
Write-Host '  ================================================' -ForegroundColor Cyan
Write-Host ''

# ── Prerequisites ─────────────────────────────────────
function Test-Command($cmd) { $null -ne (Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host '[CHECK] Validating prerequisites...' -ForegroundColor Yellow
if (-not (Test-Command 'python')) { Write-Host '  [ERROR] Python not in PATH' -ForegroundColor Red; $Errors++ }
else { Write-Host "  Python: $(python --version 2>&1)" -ForegroundColor Green }

if (-not (Test-Command 'node')) { Write-Host '  [ERROR] Node.js not in PATH' -ForegroundColor Red; $Errors++ }
else { Write-Host "  Node: $(node --version)" -ForegroundColor Green }

if (-not (Test-Command 'npm')) { Write-Host '  [ERROR] npm not in PATH' -ForegroundColor Red; $Errors++ }
else { Write-Host "  npm: $(npm --version)" -ForegroundColor Green }

if ($Errors -gt 0) {
    Write-Host "`n[FATAL] $Errors prerequisite(s) missing." -ForegroundColor Red
    exit 1
}
Write-Host ''

# ── STAGE 1: Backend ─────────────────────────────────
function Build-Server {
    Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
    Write-Host ' STAGE 1: Building Backend Server (PyInstaller)' -ForegroundColor Cyan
    Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
    Write-Host ''

    Push-Location $ServerDir
    try {
        if (-not $SkipDeps) {
            Write-Host '[1.1] Installing Python dependencies...' -ForegroundColor Yellow
            & pip install -r requirements.txt --quiet 2>&1 | Out-Null
            & pip install pyinstaller --quiet 2>&1 | Out-Null
        }

        Write-Host '[1.2] Cleaning previous build...' -ForegroundColor Yellow
        if (Test-Path 'dist') { Remove-Item -Recurse -Force 'dist' }
        if (Test-Path 'build') { Remove-Item -Recurse -Force 'build' }

        Write-Host '[1.3] Running PyInstaller (2-5 min)...' -ForegroundColor Yellow
        & pyinstaller CommClient-Server.spec --noconfirm
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

        $exe = Join-Path $ServerDist 'CommClient-Server.exe'
        if (-not (Test-Path $exe)) { throw 'Server exe not found after build' }

        $size = (Get-Item $exe).Length / 1MB
        Write-Host "[1.4] Server build complete ($([math]::Round($size, 1)) MB)" -ForegroundColor Green
        Write-Host "  Output: $ServerDist" -ForegroundColor Gray
    }
    catch {
        Write-Host "[ERROR] $_" -ForegroundColor Red
        $script:Errors++
    }
    finally { Pop-Location }
    Write-Host ''
}

# ── STAGE 2: Frontend ────────────────────────────────
function Build-Desktop {
    Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
    Write-Host ' STAGE 2: Building Desktop Frontend (Vite)' -ForegroundColor Cyan
    Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
    Write-Host ''

    Push-Location $DesktopDir
    try {
        if (-not $SkipDeps) {
            Write-Host '[2.1] Installing Node dependencies...' -ForegroundColor Yellow
            if (-not (Test-Path 'node_modules')) {
                & npm install
                if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
            } else {
                Write-Host '  node_modules exists, skipping' -ForegroundColor Gray
            }
        }

        Write-Host '[2.2] Type checking...' -ForegroundColor Yellow
        & npx tsc --noEmit 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '  [WARN] TypeScript errors — continuing' -ForegroundColor DarkYellow
        }

        Write-Host '[2.3] Building with Vite...' -ForegroundColor Yellow
        & npx vite build
        if ($LASTEXITCODE -ne 0) { throw 'Vite build failed' }

        Write-Host '[2.4] Desktop frontend build complete' -ForegroundColor Green
        Write-Host "  Output: $DesktopDir\dist-electron" -ForegroundColor Gray
    }
    catch {
        Write-Host "[ERROR] $_" -ForegroundColor Red
        $script:Errors++
    }
    finally { Pop-Location }
    Write-Host ''
}

# ── STAGE 3: Installer ───────────────────────────────
function Build-Installer {
    Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
    Write-Host ' STAGE 3: Packaging Windows Installer (NSIS)' -ForegroundColor Cyan
    Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
    Write-Host ''

    $serverExe = Join-Path $ServerDist 'CommClient-Server.exe'
    if (-not (Test-Path $serverExe)) {
        Write-Host "[ERROR] Server build not found: $serverExe" -ForegroundColor Red
        Write-Host '  Run: .\build.ps1 -Target server' -ForegroundColor Gray
        $script:Errors++
        return
    }

    Push-Location $DesktopDir
    try {
        if (Test-Path 'release') {
            Write-Host '[3.1] Cleaning previous release...' -ForegroundColor Yellow
            Remove-Item -Recurse -Force 'release'
        }

        Write-Host '[3.2] Running electron-builder (NSIS)...' -ForegroundColor Yellow
        & npx electron-builder --win --config
        if ($LASTEXITCODE -ne 0) { throw 'electron-builder failed' }

        Write-Host '[3.3] Installer build complete' -ForegroundColor Green
        Write-Host ''

        # List artifacts
        Write-Host ' Release artifacts:' -ForegroundColor Cyan
        Get-ChildItem $ReleaseDir -Filter '*.exe' -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host "  $($_.Name) ($([math]::Round($_.Length / 1MB, 1)) MB)" -ForegroundColor White }
    }
    catch {
        Write-Host "[ERROR] $_" -ForegroundColor Red
        $script:Errors++
    }
    finally { Pop-Location }
    Write-Host ''
}

# ── Execute ──────────────────────────────────────────
switch ($Target) {
    'server'    { Build-Server }
    'desktop'   { Build-Desktop }
    'installer' { Build-Installer }
    'all'       { Build-Server; Build-Desktop; Build-Installer }
}

# ── Summary ──────────────────────────────────────────
$Duration = (Get-Date) - $StartTime
Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
if ($Errors -gt 0) {
    Write-Host "  BUILD FINISHED WITH $Errors ERROR(S)" -ForegroundColor Red
} else {
    Write-Host '  BUILD SUCCESSFUL' -ForegroundColor Green
}
Write-Host "  Duration: $([math]::Round($Duration.TotalMinutes, 1)) minutes" -ForegroundColor Gray
Write-Host '═══════════════════════════════════════════════' -ForegroundColor Cyan
Write-Host ''

if ($Errors -eq 0 -and (Test-Path $ReleaseDir)) {
    Start-Process explorer.exe $ReleaseDir
}

exit $Errors
