#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Self-sign Helen-Server.exe + installer with a locally-generated
    code signing certificate. For internal LAN deployments only.

.DESCRIPTION
    Generates an in-memory CodeSigning certificate, signs the artefacts,
    and (optionally) imports the public part into the local Trusted Root
    store so SmartScreen / Defender stop warning on machines you control.

    This is *not* a substitute for a public CA-issued certificate — but
    for a 100% private LAN install where you administer every endpoint,
    self-signing eliminates the "Unknown Publisher" prompt with zero
    annual cost and zero internet dependency.

.PARAMETER CommonName
    Subject CommonName for the certificate. Default: "Helen Project Internal".

.PARAMETER ImportToTrustedRoot
    If $true, imports the cert into LocalMachine\Root so signed binaries
    are silently trusted on this machine. Default: $false (sign only).

.EXAMPLE
    PS> .\self-sign-helen.ps1
    # Signs all Helen artefacts with a fresh self-signed cert.

.EXAMPLE
    PS> .\self-sign-helen.ps1 -ImportToTrustedRoot $true
    # Also installs the cert as trusted root on this PC.
#>

param(
    [string]$CommonName = "Helen Project Internal",
    [bool]$ImportToTrustedRoot = $false
)

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# ── 1. Locate or create the cert ────────────────────────────
Write-Host "[*] Searching for existing Helen code-signing cert..." -ForegroundColor Cyan
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
        Where-Object { $_.Subject -like "*$CommonName*" } |
        Select-Object -First 1

if (-not $cert) {
    Write-Host "[*] No existing cert. Generating a new self-signed cert..." -ForegroundColor Cyan
    $cert = New-SelfSignedCertificate `
        -Subject "CN=$CommonName" `
        -Type CodeSigningCert `
        -KeyUsage DigitalSignature `
        -KeyLength 4096 `
        -KeyAlgorithm RSA `
        -HashAlgorithm SHA256 `
        -NotAfter (Get-Date).AddYears(10) `
        -CertStoreLocation Cert:\CurrentUser\My `
        -KeyExportPolicy Exportable
    Write-Host "[+] Created cert: $($cert.Thumbprint)" -ForegroundColor Green
} else {
    Write-Host "[+] Reusing cert: $($cert.Thumbprint)" -ForegroundColor Green
}

# ── 2. Optional: import as trusted root ─────────────────────
if ($ImportToTrustedRoot) {
    Write-Host "[*] Importing public cert into LocalMachine\Root..." -ForegroundColor Cyan
    $cerPath = Join-Path $env:TEMP "helen-pub.cer"
    Export-Certificate -Cert $cert -FilePath $cerPath -Force | Out-Null
    Import-Certificate -FilePath $cerPath -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    Remove-Item $cerPath -Force
    Write-Host "[+] Imported as trusted root." -ForegroundColor Green

    # Also ensure CodeSigning trust on this machine
    $rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store("TrustedPublisher", "LocalMachine")
    $rootStore.Open("ReadWrite")
    $rootStore.Add($cert)
    $rootStore.Close()
    Write-Host "[+] Imported into TrustedPublisher store." -ForegroundColor Green
}

# ── 3. Sign artefacts ───────────────────────────────────────
$WorkspaceRoot = Split-Path -Parent $ProjectRoot
$targets = @(
    "$ProjectRoot\dist\Helen-Server\Helen-Server.exe",
    "$ProjectRoot\Helen-Server-Setup-1.0.0.exe",
    "$ProjectRoot\dist\CommClient-Admin\CommClient-Admin.exe",
    "$WorkspaceRoot\Helen-Rendezvous\Helen-Rendezvous-Setup-1.0.0.exe",
    "$WorkspaceRoot\Helen-Rendezvous\dist\Helen-Rendezvous\Helen-Rendezvous.exe",
    "$WorkspaceRoot\Helen-Router\dist\Helen-Router\Helen-Router.exe",
    "$WorkspaceRoot\Helen-Router\Helen-Router-Setup-1.0.0.exe",
    "$WorkspaceRoot\CommClient-Desktop\release\Helen Desktop Setup 1.0.0.exe"
)

# RFC 3161 timestamp servers — try a private one first if you have it,
# then skip timestamping entirely (signature still valid for ~10 years).
# Setting $TimestampUrl = "" disables timestamping (LAN-only, no internet).
$TimestampUrl = ""

foreach ($t in $targets) {
    if (-not (Test-Path $t)) {
        Write-Host "[!] Skip (not built): $t" -ForegroundColor Yellow
        continue
    }
    Write-Host "[*] Signing: $t" -ForegroundColor Cyan
    if ($TimestampUrl) {
        Set-AuthenticodeSignature -FilePath $t -Certificate $cert `
            -TimestampServer $TimestampUrl `
            -HashAlgorithm SHA256 | Out-Null
    } else {
        Set-AuthenticodeSignature -FilePath $t -Certificate $cert `
            -HashAlgorithm SHA256 | Out-Null
    }

    $sig = Get-AuthenticodeSignature $t
    if ($sig.Status -eq "Valid" -or $sig.Status -eq "UnknownError") {
        # UnknownError = "self-signed but cryptographically valid" before
        # the cert is imported as trusted root. After import → Valid.
        Write-Host "[+] Signed OK ($($sig.Status))" -ForegroundColor Green
    } else {
        Write-Host "[-] Sign failed: $($sig.StatusMessage)" -ForegroundColor Red
    }
}

Write-Host
Write-Host "============================================="  -ForegroundColor Cyan
Write-Host "  Self-signing complete."  -ForegroundColor Cyan
Write-Host "============================================="  -ForegroundColor Cyan
Write-Host
Write-Host "  Cert thumbprint: $($cert.Thumbprint)"
Write-Host "  Subject:         $($cert.Subject)"
Write-Host "  Valid until:     $($cert.NotAfter)"
Write-Host
if (-not $ImportToTrustedRoot) {
    Write-Host "  TIP: To make Windows trust this cert silently on every machine"
    Write-Host "       in your LAN, rerun with:"
    Write-Host "         .\self-sign-helen.ps1 -ImportToTrustedRoot `$true"
    Write-Host "       (or push the .cer to the domain via Group Policy)."
    Write-Host
}
