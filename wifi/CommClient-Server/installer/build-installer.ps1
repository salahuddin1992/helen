<#
.SYNOPSIS
  Phase 3 / Module Q — Production installer build orchestrator.

.DESCRIPTION
  Builds the Helen CommClient server (PyInstaller), desktop (electron-builder),
  optionally signs the binaries, then packages BOTH a NSIS .exe installer
  and a WiX .msi installer. Emits a SHA-256 manifest alongside the output.

.PARAMETER Version
  Version string written into installer metadata.

.PARAMETER CertPath / CertPass
  When both are set, every binary is signed via signtool.

.PARAMETER SkipServer / SkipDesktop / SkipNSIS / SkipMSI
  Toggle individual build steps for fast iteration.

.EXAMPLE
  .\build-installer.ps1 -Version 1.3.0 -CertPath C:\certs\ev.pfx -CertPass $env:CERT_PASS
#>
[CmdletBinding()]
param(
  [string]$Version = "1.3.0",
  [string]$CertPath = "",
  [string]$CertPass = "",
  [string]$TimestampUrl = "http://timestamp.digicert.com",
  [switch]$SkipServer,
  [switch]$SkipDesktop,
  [switch]$SkipNSIS,
  [switch]$SkipMSI,
  [string]$NsisExe = "C:\Program Files (x86)\NSIS\makensis.exe",
  [string]$WixCandle = "C:\Program Files (x86)\WiX Toolset v3.11\bin\candle.exe",
  [string]$WixLight  = "C:\Program Files (x86)\WiX Toolset v3.11\bin\light.exe",
  [string]$SignTool  = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$serverRoot = Join-Path $repoRoot "CommClient-Server"
$desktopRoot = Join-Path $repoRoot "CommClient-Desktop"
$distRoot = Join-Path $serverRoot "dist"
$installerOut = Join-Path $distRoot "installer-v2"
$nsiScript = Join-Path $PSScriptRoot "v2.nsi"
$wxsScript = Join-Path $PSScriptRoot "helen-installer.msi.wxs"

Write-Host "=== Helen Installer Build v$Version ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $installerOut | Out-Null

# ──────────────────────────────────────────────────────────────
# 1. Server bundle (PyInstaller)
# ──────────────────────────────────────────────────────────────
if (-not $SkipServer) {
  Write-Host "`n[1/4] Building server via PyInstaller…" -ForegroundColor Yellow
  Push-Location $serverRoot
  try {
    python -m pip install -q --upgrade pip pyinstaller
    python -m pip install -q -r requirements.txt
    python -m PyInstaller `
      --noconfirm `
      --clean `
      --name helen-server `
      --onefile `
      --distpath "$distRoot\server" `
      --workpath "$distRoot\build\server" `
      app\main.py
  } finally { Pop-Location }
}

# ──────────────────────────────────────────────────────────────
# 2. Desktop bundle (electron-builder)
# ──────────────────────────────────────────────────────────────
if (-not $SkipDesktop) {
  Write-Host "`n[2/4] Building desktop via electron-builder…" -ForegroundColor Yellow
  Push-Location $desktopRoot
  try {
    npm ci
    npm run build
    npx electron-builder --win --x64 --publish never
  } finally { Pop-Location }
}

# ──────────────────────────────────────────────────────────────
# 3. Code-sign helper
# ──────────────────────────────────────────────────────────────
function Invoke-Sign($path) {
  if (-not $CertPath -or -not (Test-Path $CertPath)) {
    Write-Host "  [sign] skipped (no cert provided): $path"
    return
  }
  if (-not (Test-Path $SignTool)) {
    Write-Warning "signtool.exe not found at $SignTool — install Windows SDK."
    return
  }
  & $SignTool sign /f $CertPath /p $CertPass /tr $TimestampUrl /td sha256 /fd sha256 $path
  if ($LASTEXITCODE -ne 0) { throw "signtool failed for $path" }
}

# Sign primary EXEs before packaging.
Get-ChildItem -Path "$distRoot\server" -Filter "*.exe" -ErrorAction SilentlyContinue |
  ForEach-Object { Invoke-Sign $_.FullName }
Get-ChildItem -Path "$desktopRoot\dist" -Recurse -Filter "Helen.exe" -ErrorAction SilentlyContinue |
  ForEach-Object { Invoke-Sign $_.FullName }

# ──────────────────────────────────────────────────────────────
# 4a. NSIS installer
# ──────────────────────────────────────────────────────────────
if (-not $SkipNSIS) {
  Write-Host "`n[3/4] Packaging NSIS installer…" -ForegroundColor Yellow
  if (-not (Test-Path $NsisExe)) {
    Write-Warning "NSIS not found at $NsisExe — skipping. Install from https://nsis.sourceforge.io/"
  } else {
    & $NsisExe "/DVERSION=$Version" $nsiScript
    if ($LASTEXITCODE -ne 0) { throw "makensis failed" }
    Get-ChildItem -Path $installerOut -Filter "helen-installer-*.exe" |
      ForEach-Object { Invoke-Sign $_.FullName }
  }
}

# ──────────────────────────────────────────────────────────────
# 4b. WiX MSI
# ──────────────────────────────────────────────────────────────
if (-not $SkipMSI) {
  Write-Host "`n[4/4] Packaging WiX MSI…" -ForegroundColor Yellow
  if (-not (Test-Path $WixCandle) -or -not (Test-Path $WixLight)) {
    Write-Warning "WiX Toolset not found — skipping MSI. Install v3.11+."
  } else {
    $wixobj = Join-Path $installerOut "helen-installer.wixobj"
    $msi    = Join-Path $installerOut "helen-installer-$Version.msi"
    & $WixCandle -nologo -arch x64 -dVersion=$Version `
        -out $wixobj $wxsScript
    if ($LASTEXITCODE -ne 0) { throw "candle.exe failed" }
    & $WixLight  -nologo -ext WixUIExtension -out $msi $wixobj
    if ($LASTEXITCODE -ne 0) { throw "light.exe failed" }
    Invoke-Sign $msi
  }
}

# ──────────────────────────────────────────────────────────────
# Manifest
# ──────────────────────────────────────────────────────────────
Write-Host "`nGenerating SHA-256 manifest…" -ForegroundColor Yellow
$manifest = @{
  version = $Version
  built_at = (Get-Date).ToString("o")
  files = @()
}
Get-ChildItem -Path $installerOut -File | ForEach-Object {
  $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
  $manifest.files += @{
    name = $_.Name
    size = $_.Length
    sha256 = $hash
  }
}
$manifest | ConvertTo-Json -Depth 4 |
  Out-File (Join-Path $installerOut "manifest.json") -Encoding utf8

Write-Host "`n=== DONE ===" -ForegroundColor Green
Write-Host "Output: $installerOut"
Get-ChildItem -Path $installerOut -File | ForEach-Object {
  Write-Host (" - {0,-50} {1,10:N0} bytes" -f $_.Name, $_.Length)
}
