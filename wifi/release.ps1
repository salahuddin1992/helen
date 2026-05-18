#Requires -Version 5.1
<#
.SYNOPSIS
    CommClient — Release Script

.DESCRIPTION
    Creates a versioned release:
      1. Bumps version in package.json (semver)
      2. Runs full build pipeline
      3. Generates SHA256 checksums
      4. Creates release manifest (JSON)
      5. Copies artifacts to release/<version>/

.PARAMETER Version
    Target version (e.g., "1.2.0"). If omitted, bumps patch.

.PARAMETER BumpType
    Semver bump type: 'patch' (default), 'minor', 'major'

.PARAMETER SkipBuild
    Skip the build step (use existing build artifacts)

.EXAMPLE
    .\release.ps1                          # bump patch, full build
    .\release.ps1 -Version "2.0.0"        # explicit version
    .\release.ps1 -BumpType minor         # bump minor
    .\release.ps1 -SkipBuild              # just package existing build
#>

param(
    [string]$Version,
    [ValidateSet('patch', 'minor', 'major')]
    [string]$BumpType = 'patch',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = Join-Path $Root 'CommClient-Desktop'
$ServerDir = Join-Path $Root 'CommClient-Server'
$PackageJson = Join-Path $DesktopDir 'package.json'

Write-Host ''
Write-Host '  ================================================' -ForegroundColor Magenta
Write-Host '   CommClient — Release Pipeline' -ForegroundColor Magenta
Write-Host "   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
Write-Host '  ================================================' -ForegroundColor Magenta
Write-Host ''

# ── Version Management ────────────────────────────────
function Get-CurrentVersion {
    $pkg = Get-Content $PackageJson -Raw | ConvertFrom-Json
    return $pkg.version
}

function Set-Version([string]$ver) {
    $pkg = Get-Content $PackageJson -Raw | ConvertFrom-Json
    $pkg.version = $ver
    $pkg | ConvertTo-Json -Depth 10 | Set-Content $PackageJson -Encoding UTF8
}

function Bump-Version([string]$current, [string]$type) {
    $parts = $current -split '\.'
    switch ($type) {
        'major' { return "$([int]$parts[0] + 1).0.0" }
        'minor' { return "$($parts[0]).$([int]$parts[1] + 1).0" }
        'patch' { return "$($parts[0]).$($parts[1]).$([int]$parts[2] + 1)" }
    }
}

$CurrentVersion = Get-CurrentVersion
Write-Host "[VERSION] Current: $CurrentVersion" -ForegroundColor Yellow

if ($Version) {
    $NewVersion = $Version
} else {
    $NewVersion = Bump-Version $CurrentVersion $BumpType
}

Write-Host "[VERSION] New: $NewVersion ($BumpType)" -ForegroundColor Green
Set-Version $NewVersion
Write-Host "[VERSION] Updated package.json" -ForegroundColor Gray
Write-Host ''

# ── Build ─────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Host '[BUILD] Running full build pipeline...' -ForegroundColor Yellow
    Write-Host ''
    & (Join-Path $Root 'build.ps1') -Target all
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[ERROR] Build failed — aborting release' -ForegroundColor Red
        Set-Version $CurrentVersion  # rollback
        exit 1
    }
}

# ── Collect Artifacts ─────────────────────────────────
$BuildRelease = Join-Path $DesktopDir 'release'
$ReleaseDir = Join-Path $Root "releases\v$NewVersion"

Write-Host "[RELEASE] Collecting artifacts → $ReleaseDir" -ForegroundColor Yellow

if (-not (Test-Path $BuildRelease)) {
    Write-Host '[ERROR] No build artifacts found in release/' -ForegroundColor Red
    exit 1
}

# Create release directory
New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

# Copy installer exe(s)
$Installers = Get-ChildItem $BuildRelease -Filter '*.exe' -ErrorAction SilentlyContinue
if ($Installers.Count -eq 0) {
    Write-Host '[ERROR] No installer .exe found' -ForegroundColor Red
    exit 1
}

foreach ($file in $Installers) {
    Copy-Item $file.FullName $ReleaseDir
    Write-Host "  Copied: $($file.Name) ($([math]::Round($file.Length / 1MB, 1)) MB)" -ForegroundColor Gray
}

# Copy yml metadata if exists
Get-ChildItem $BuildRelease -Filter '*.yml' -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-Item $_.FullName $ReleaseDir }

# ── Checksums ─────────────────────────────────────────
Write-Host '[CHECKSUM] Generating SHA256...' -ForegroundColor Yellow

$ChecksumFile = Join-Path $ReleaseDir 'SHA256SUMS.txt'
$checksums = @()

Get-ChildItem $ReleaseDir -File | Where-Object { $_.Extension -ne '.txt' -and $_.Extension -ne '.json' } | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    $line = "$hash  $($_.Name)"
    $checksums += $line
    Write-Host "  $line" -ForegroundColor Gray
}

$checksums | Out-File $ChecksumFile -Encoding UTF8
Write-Host ''

# ── Release Manifest ──────────────────────────────────
Write-Host '[MANIFEST] Generating release.json...' -ForegroundColor Yellow

$manifest = @{
    product   = 'CommClient'
    version   = $NewVersion
    platform  = 'win32-x64'
    timestamp = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')
    build     = @{
        electron = (& node -e "console.log(require('$DesktopDir/node_modules/electron/package.json').version)" 2>$null)
        node     = (node --version)
        python   = (python --version 2>&1).ToString().Replace('Python ', '')
    }
    artifacts = @()
}

Get-ChildItem $ReleaseDir -Filter '*.exe' | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    $manifest.artifacts += @{
        filename = $_.Name
        size     = $_.Length
        sha256   = $hash
    }
}

$manifest | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $ReleaseDir 'release.json') -Encoding UTF8
Write-Host ''

# ── Changelog stub ────────────────────────────────────
$ChangelogPath = Join-Path $ReleaseDir 'CHANGELOG.md'
if (-not (Test-Path $ChangelogPath)) {
    $changelogContent = @"
# CommClient v$NewVersion

**Release Date:** $(Get-Date -Format 'yyyy-MM-dd')
**Platform:** Windows x64

## What's New

- [Add release notes here]

## Installation

1. Download ``CommClient-Setup-$NewVersion.exe``
2. Run the installer (no admin required for per-user install)
3. Launch CommClient from Start Menu or Desktop shortcut
4. The backend server starts automatically

## System Requirements

- Windows 10/11 (x64)
- 4GB RAM minimum
- LAN/WiFi network connection

## Checksums

See ``SHA256SUMS.txt`` for file integrity verification.
"@
    $changelogContent | Set-Content $ChangelogPath -Encoding UTF8
    Write-Host "[CHANGELOG] Created stub at CHANGELOG.md" -ForegroundColor Gray
    Write-Host "  Edit $ChangelogPath with release notes" -ForegroundColor DarkYellow
}

# ── Summary ───────────────────────────────────────────
Write-Host ''
Write-Host '  ================================================' -ForegroundColor Magenta
Write-Host "   Release v$NewVersion Ready" -ForegroundColor Green
Write-Host "   Location: $ReleaseDir" -ForegroundColor Gray
Write-Host '  ================================================' -ForegroundColor Magenta
Write-Host ''
Write-Host '  Release contents:' -ForegroundColor Cyan
Get-ChildItem $ReleaseDir | ForEach-Object {
    $size = if ($_.Length -gt 1MB) { "$([math]::Round($_.Length / 1MB, 1)) MB" } else { "$([math]::Round($_.Length / 1KB, 1)) KB" }
    Write-Host "    $($_.Name) ($size)"
}
Write-Host ''
Write-Host '  Next steps:' -ForegroundColor Yellow
Write-Host '    1. Edit CHANGELOG.md with release notes' -ForegroundColor Gray
Write-Host '    2. Test installer on a clean Windows machine' -ForegroundColor Gray
Write-Host '    3. Distribute via LAN share or USB' -ForegroundColor Gray
Write-Host ''

Start-Process explorer.exe $ReleaseDir
