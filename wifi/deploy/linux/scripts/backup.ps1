<#
.SYNOPSIS
    Helen-Server backup for Windows. Mirror of backup.sh.

.DESCRIPTION
    Creates a timestamped ZIP of .env + _internal/data, retains $KeepDays.

.EXAMPLE
    PS> .\backup.ps1
    PS> .\backup.ps1 -OutDir D:\backups\helen -KeepDays 30
#>
param(
    [string]$InstallDir = "C:\Program Files\Helen-Server",
    [string]$OutDir     = "C:\ProgramData\Helen\backups",
    [int]   $KeepDays   = 14
)

if (-not (Test-Path $InstallDir)) {
    Write-Error "InstallDir not found: $InstallDir"
    exit 1
}
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

$ts   = Get-Date -Format "yyyyMMdd-HHmmss"
$out  = Join-Path $OutDir "helen-backup-$ts.zip"
$tmp  = Join-Path $env:TEMP "helen-backup-$ts"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

# Try SQLite online backup if sqlite3.exe is on PATH
$db = Join-Path $InstallDir "_internal\data\commclient.db"
$sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
if ($sqlite -and (Test-Path $db)) {
    & sqlite3.exe $db ".backup '$tmp\commclient.db'" 2>$null
} elseif (Test-Path $db) {
    Copy-Item $db (Join-Path $tmp "commclient.db") -Force
}

# Stage .env + data/
$staging = Join-Path $tmp "stage"
New-Item -ItemType Directory -Path $staging -Force | Out-Null

if (Test-Path "$InstallDir\.env") {
    Copy-Item "$InstallDir\.env" $staging
}
if (Test-Path "$InstallDir\_internal\data") {
    Copy-Item -Recurse "$InstallDir\_internal\data" "$staging\data"
    if (Test-Path "$tmp\commclient.db") {
        Copy-Item "$tmp\commclient.db" "$staging\data\commclient.db" -Force
    }
}

Compress-Archive -Path "$staging\*" -DestinationPath $out -CompressionLevel Optimal
Remove-Item -Recurse -Force $tmp

Write-Host "[+] Backup written: $out  ($([math]::Round((Get-Item $out).Length/1MB,1)) MB)"

# Rotate
Get-ChildItem $OutDir -Filter "helen-backup-*.zip" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Force -Verbose
