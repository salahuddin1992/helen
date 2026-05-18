#requires -Version 5.1
<#
.SYNOPSIS
  Orchestrate every optimized build: PyInstaller (optimized) + Nuitka +
  delta patch (if a previous release is on disk).

.DESCRIPTION
  Produces three artifacts in parallel-friendly order:

    1. dist-optimized/Helen-Server/        (PyInstaller, UPX + excludes)
    2. dist-nuitka/Helen-Server/           (Nuitka, --standalone)
    3. dist-deltas/<from>_to_<to>/         (binary patch if previous build
                                            present at dist-prev/)

  Prints a size-comparison table at the end.

.PARAMETER FromVersion
  Old version label used for delta naming. Optional.

.PARAMETER ToVersion
  New version label used for delta naming. Default = git short-rev or "dev".

.PARAMETER SkipNuitka
  Skip Nuitka build (saves 6-10 minutes).

.PARAMETER SkipPyInstaller
  Skip PyInstaller optimized build.
#>

[CmdletBinding()]
param(
    [string] $FromVersion = $null,
    [string] $ToVersion   = $null,
    [switch] $SkipNuitka,
    [switch] $SkipPyInstaller
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

if (-not $ToVersion) {
    try {
        $ToVersion = (& git -C $root rev-parse --short HEAD 2>$null).Trim()
    } catch { }
    if (-not $ToVersion) { $ToVersion = "dev" }
}

function H($title) {
    Write-Host ""
    Write-Host ("═" * 72) -ForegroundColor DarkCyan
    Write-Host "  $title" -ForegroundColor Cyan
    Write-Host ("═" * 72) -ForegroundColor DarkCyan
}

function Size-MB($path) {
    if (-not (Test-Path $path)) { return 0 }
    [math]::Round((Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum / 1MB, 2)
}

$results = [ordered]@{}

# ── 1. PyInstaller optimized ──────────────────────────────────────────
if (-not $SkipPyInstaller) {
    H "1/3  PyInstaller (optimized spec)"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Push-Location $root
    try {
        & pyinstaller --noconfirm --clean CommClient-Server.optimized.spec
        if ($LASTEXITCODE -ne 0) { throw "pyinstaller exited $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
    $sw.Stop()
    $results["PyInstaller (optimized)"] = @{
        path = Join-Path $root "dist-optimized\Helen-Server"
        size = Size-MB (Join-Path $root "dist-optimized\Helen-Server")
        elapsed_s = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
} else {
    H "1/3  PyInstaller (skipped)"
}

# ── 2. Nuitka ────────────────────────────────────────────────────────
if (-not $SkipNuitka) {
    H "2/3  Nuitka (--standalone --lto)"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & (Join-Path $root "build-nuitka.ps1") -NoCompare
    if ($LASTEXITCODE -ne 0) { throw "build-nuitka.ps1 exited $LASTEXITCODE" }
    $sw.Stop()
    $results["Nuitka"] = @{
        path = Join-Path $root "dist-nuitka\Helen-Server"
        size = Size-MB (Join-Path $root "dist-nuitka\Helen-Server")
        elapsed_s = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
} else {
    H "2/3  Nuitka (skipped)"
}

# ── 3. Delta vs previous release ─────────────────────────────────────
H "3/3  Delta patch (vs dist-prev/)"

$prevExe = Join-Path $root "dist-prev\Helen-Server\Helen-Server.exe"
$newExe  = Join-Path $root "dist-optimized\Helen-Server\Helen-Server.exe"

if (-not $FromVersion) { $FromVersion = "prev" }

if ((Test-Path $prevExe) -and (Test-Path $newExe)) {
    & python (Join-Path $root "delta-update-builder.py") `
        --old $prevExe `
        --new $newExe `
        --from-version $FromVersion `
        --to-version $ToVersion `
        --output (Join-Path $root "dist-deltas")
    if ($LASTEXITCODE -eq 0) {
        $deltaDir = Join-Path $root "dist-deltas\${FromVersion}_to_${ToVersion}"
        $results["Delta patch"] = @{
            path = $deltaDir
            size = Size-MB $deltaDir
            elapsed_s = 0
        }
    }
} else {
    Write-Host "  no dist-prev/Helen-Server/Helen-Server.exe — skipping delta" -ForegroundColor Yellow
}

# ── Summary table ────────────────────────────────────────────────────
H "Summary"

"{0,-30} {1,12} {2,10} {3}" -f "Backend", "Size (MB)", "Time (s)", "Path"
"-" * 72
foreach ($k in $results.Keys) {
    $r = $results[$k]
    "{0,-30} {1,12} {2,10} {3}" -f $k, $r.size, $r.elapsed_s, $r.path
}

Write-Host ""
Write-Host "[build-all-optimized] done." -ForegroundColor Green
