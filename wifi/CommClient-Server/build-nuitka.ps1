#requires -Version 5.1
<#
.SYNOPSIS
  Build Helen-Server with Nuitka instead of PyInstaller.

.DESCRIPTION
  Nuitka compiles Python to C and produces a real native binary that is
  typically 30-40% smaller and 10-20% faster cold-startup than PyInstaller
  on Windows. Trade-off: build time is 10x slower (~6-10 min vs ~45 s).

  Use Nuitka for release builds (size + perf matter). Use PyInstaller for
  iteration / dev builds (speed matters).

.PARAMETER MinGW64
  Use the bundled MinGW64 toolchain (recommended on machines without MSVC).

.PARAMETER OutDir
  Output directory. Defaults to ./dist-nuitka.

.PARAMETER NoCompare
  Skip the size-comparison report vs ./dist/Helen-Server/.

.EXAMPLE
  .\build-nuitka.ps1 -MinGW64
#>

[CmdletBinding()]
param(
  [switch] $MinGW64,
  [string] $OutDir = "$PSScriptRoot\dist-nuitka",
  [switch] $NoCompare
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

function Write-Info($msg) { Write-Host "[nuitka-build] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[nuitka-build] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[nuitka-build] $msg" -ForegroundColor Yellow }

# ── Preflight ──────────────────────────────────────────────────────────
$pyExe = (Get-Command python -ErrorAction SilentlyContinue)?.Path
if (-not $pyExe) { throw "python.exe not found in PATH" }

$pyVer = & $pyExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Info "python = $pyVer ($pyExe)"

$nuitkaCheck = & $pyExe -m pip show nuitka 2>$null
if (-not $nuitkaCheck) {
    Write-Warn2 "Nuitka not installed — installing now..."
    & $pyExe -m pip install --quiet nuitka ordered-set zstandard
}
$nuitkaVer = (& $pyExe -m nuitka --version 2>&1 | Select-Object -First 1)
Write-Info "nuitka = $nuitkaVer"

# Clean previous build
if (Test-Path $OutDir) {
    Write-Info "removing previous $OutDir"
    Remove-Item $OutDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ── Build args ─────────────────────────────────────────────────────────
$args = @(
    "-m", "nuitka",
    "--standalone",
    "--windows-console-mode=force",
    "--assume-yes-for-downloads",
    "--enable-plugin=anti-bloat",
    "--enable-plugin=pylint-warnings",
    "--include-package=app",
    "--include-package=uvicorn",
    "--include-package=fastapi",
    "--include-package=starlette",
    "--include-package=pydantic",
    "--include-package=pydantic_core",
    "--include-package=pydantic_settings",
    "--include-package=socketio",
    "--include-package=engineio",
    "--include-package=sqlalchemy",
    "--include-package=aiosqlite",
    "--include-package=alembic",
    "--include-package=zeroconf",
    "--include-package=structlog",
    "--include-package=email_validator",
    "--include-package=multipart",
    "--include-package=httptools",
    "--include-package=websockets",
    "--include-package=wsproto",
    "--include-package=psutil",
    "--include-data-dir=app/static=app/static",
    "--include-data-dir=app/transports/config=app/transports/config",
    "--include-data-dir=migrations=migrations",
    "--include-data-files=alembic.ini=alembic.ini",
    "--output-dir=$OutDir",
    "--output-filename=Helen-Server.exe",
    "--remove-output",          # nuke .build/ intermediate at end
    "--lto=yes",
    "--prefer-source-code"
)

# Admin/ static dir is optional
if (Test-Path "$PSScriptRoot\admin") {
    $args += "--include-data-dir=admin=admin"
}

# iOS/Admin web simulators (LAN admin panel)
foreach ($simParent in @('iOS', 'iOS-Admin')) {
    $simSrc = Join-Path (Split-Path $PSScriptRoot -Parent) "$simParent\web-simulator"
    if (Test-Path $simSrc) {
        $args += "--include-data-dir=$simSrc=$simParent/web-simulator"
    }
}

if ($MinGW64) { $args += "--mingw64" }

$args += "run.py"

Write-Info "starting nuitka build (this may take 6-10 minutes)..."
$sw = [System.Diagnostics.Stopwatch]::StartNew()

& $pyExe @args
$rc = $LASTEXITCODE
$sw.Stop()

if ($rc -ne 0) {
    throw "Nuitka exited with code $rc"
}

Write-Ok ("build completed in {0:N1} s" -f $sw.Elapsed.TotalSeconds)

# ── Size + manifest ────────────────────────────────────────────────────
$bin = Join-Path $OutDir "run.dist"
if (-not (Test-Path $bin)) {
    Write-Warn2 "expected output dir not found: $bin"
} else {
    # Rename to match PyInstaller convention
    $target = Join-Path $OutDir "Helen-Server"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Rename-Item $bin $target
    Write-Ok "renamed → $target"

    $totalBytes = (Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum
    $totalMB = [math]::Round($totalBytes / 1MB, 2)
    Write-Ok "total size: $totalMB MB"

    # SHA-256 manifest
    $manifestPath = Join-Path $OutDir "SHA256SUMS.txt"
    $rows = Get-ChildItem $target -Recurse -File | ForEach-Object {
        $h = (Get-FileHash -Algorithm SHA256 -Path $_.FullName).Hash.ToLower()
        $rel = $_.FullName.Substring($target.Length + 1)
        "$h  $rel"
    }
    $rows | Set-Content -Encoding ascii $manifestPath
    Write-Ok "manifest written → $manifestPath ($($rows.Count) files)"
}

# ── Compare vs PyInstaller dist ────────────────────────────────────────
if (-not $NoCompare) {
    $pyiPath = Join-Path $PSScriptRoot "dist\Helen-Server"
    if (Test-Path $pyiPath) {
        $pyiBytes = (Get-ChildItem $pyiPath -Recurse -File | Measure-Object Length -Sum).Sum
        $pyiMB = [math]::Round($pyiBytes / 1MB, 2)
        $diffMB = [math]::Round(($pyiMB - $totalMB), 2)
        $pct = if ($pyiMB) { [math]::Round((($pyiMB - $totalMB) / $pyiMB) * 100, 1) } else { 0 }
        Write-Host ""
        Write-Host "  ┌───────────────────────────────────────────────────┐"
        Write-Host ("  │  PyInstaller  : {0,8} MB" -f $pyiMB)
        Write-Host ("  │  Nuitka       : {0,8} MB" -f $totalMB)
        Write-Host ("  │  Saved        : {0,8} MB  ({1}%)" -f $diffMB, $pct)
        Write-Host "  └───────────────────────────────────────────────────┘"
    } else {
        Write-Warn2 "no PyInstaller build at $pyiPath — skipping comparison"
    }
}

Write-Ok "done."
