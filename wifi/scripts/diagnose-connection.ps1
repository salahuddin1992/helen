<#
.SYNOPSIS
    CommClient connection diagnostic -- prints PASS/FAIL for every link in
    the chain (server reachable, single instance, JWT consistency, DB
    consistency, WebSocket handshake, client config sanity).

.DESCRIPTION
    Exit code 0 when every check passes, 1 otherwise. Designed to be safe
    to run against a live install -- no destructive operations.

    Usage:
        powershell -NoProfile -File scripts/diagnose-connection.ps1
        powershell -NoProfile -File scripts/diagnose-connection.ps1 -ServerUrl http://192.168.1.5:3000
#>

[CmdletBinding()]
param(
    [string]$ServerUrl = $null,
    [string]$ConfigPath = "$env:APPDATA\CommClient\config.json"
)

$ErrorActionPreference = 'Continue'
$Script:Failed = 0

function Write-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    $tag = if ($Ok) { 'PASS' } else { 'FAIL' }
    $color = if ($Ok) { 'Green' } else { 'Red' }
    Write-Host ('  [{0}] {1}' -f $tag, $Name) -ForegroundColor $color
    if ($Detail) { Write-Host "         $Detail" -ForegroundColor DarkGray }
    if (-not $Ok) { $Script:Failed++ }
}

function Section { param([string]$Title) Write-Host "`n== $Title ==" -ForegroundColor Cyan }

# -- 1. Resolve serverUrl from config (or override) ----------─
Section 'Client config'
$cfg = $null
if (Test-Path $ConfigPath) {
    try {
        $cfg = Get-Content -Raw $ConfigPath | ConvertFrom-Json
        Write-Check 'config.json found' $true $ConfigPath
        Write-Host ('         mode={0}  serverUrl={1}' -f $cfg.mode, $cfg.serverUrl) -ForegroundColor DarkGray
        Write-Host ('         allowEmbeddedServer={0}  allowLanDiscovery={1}  allowAutoServerSwitch={2}' `
            -f $cfg.allowEmbeddedServer, $cfg.allowLanDiscovery, $cfg.allowAutoServerSwitch) -ForegroundColor DarkGray
    } catch {
        Write-Check 'config.json parse' $false $_.Exception.Message
    }
} else {
    Write-Check 'config.json found' $false "missing: $ConfigPath (defaults will be created on next launch)"
}

if (-not $ServerUrl) {
    if ($cfg -and $cfg.serverUrl) { $ServerUrl = $cfg.serverUrl }
    else { $ServerUrl = 'http://127.0.0.1:3000' }
}

# -- 2. Process inventory ------------------------------------─
Section 'Process inventory'
$serverProcs = Get-Process Helen-Server -ErrorAction SilentlyContinue
$adminProcs  = Get-Process Helen-Admin  -ErrorAction SilentlyContinue
$desktopProcs = Get-Process 'Helen Desktop','Helen' -ErrorAction SilentlyContinue

$serverDetail = if ($serverProcs) { ($serverProcs | ForEach-Object { "PID $($_.Id)" }) -join ', ' } else { 'no process found' }
$adminDetail  = if ($adminProcs)  { "PID $($adminProcs.Id)" } else { 'no admin process (optional)' }
$desktopDetail = if ($desktopProcs) { "$($desktopProcs.Count) electron processes" } else { 'no desktop client' }
Write-Check 'Helen-Server.exe running' ($null -ne $serverProcs) $serverDetail
Write-Check 'Helen-Admin.exe running'  ($null -ne $adminProcs)  $adminDetail
Write-Check 'Helen Desktop running'    ($null -ne $desktopProcs) $desktopDetail

# Split-brain detection -- multiple Helen-Server instances on different ports.
if ($serverProcs -and $serverProcs.Count -gt 1) {
    $listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -in $serverProcs.Id }
    $ports = ($listeners | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object) -join ','
    Write-Check 'Single Helen-Server instance' $false (
        "$($serverProcs.Count) instances bound to ports: $ports -- split-brain risk"
    )
} else {
    Write-Check 'Single Helen-Server instance' $true
}

# -- 3. Listening ports --------------------------------------─
Section 'Listening ports'
foreach ($p in 3000, 3001, 5173, 41234) {
    $conn = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($p -eq 41234) {
        # 41234 is UDP -- skip (TCP probe always false)
        continue
    }
    if ($conn) {
        $owner = (Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Write-Check "Port $p listening" $true "owner: $owner (PID $($conn.OwningProcess))"
    } else {
        $expected = ($p -eq 3000) -or ($p -eq 5173)
        $portDetail = if ($expected) { 'expected service is down' } else { 'unused (expected)' }
        Write-Check "Port $p listening" (-not $expected) $portDetail
    }
}

# -- 4. HTTP /api/health probe --------------------------------
Section 'HTTP probes'
try {
    $r = Invoke-WebRequest -Uri "$ServerUrl/api/health" -UseBasicParsing -TimeoutSec 5
    $ok = $r.StatusCode -eq 200
    Write-Check "$ServerUrl/api/health" $ok ("HTTP $($r.StatusCode)  body: $($r.Content)")
} catch {
    Write-Check "$ServerUrl/api/health" $false $_.Exception.Message
}

# -- 5. Connection diagnostics endpoint (the new one) --------─
try {
    $diag = Invoke-RestMethod -Uri "$ServerUrl/api/connection/diagnostics" -TimeoutSec 5
    Write-Check '/api/connection/diagnostics' $true ("server_id=$($diag.serverInfo.server_id.Substring(0,8))...  online_users=$($diag.serverInfo.online_users)")
} catch {
    Write-Check '/api/connection/diagnostics' $false $_.Exception.Message
}

# -- 6. Socket.IO handshake (file:// origin) ------------------
try {
    $r = Invoke-WebRequest -Uri "$ServerUrl/socket.io/?EIO=4&transport=polling" `
        -Headers @{ Origin = 'file://' } -UseBasicParsing -TimeoutSec 5
    $ok = $r.StatusCode -eq 200 -and $r.Content -match '"sid"'
    Write-Check 'Socket.IO accepts file:// origin' $ok "HTTP $($r.StatusCode)"
} catch {
    Write-Check 'Socket.IO accepts file:// origin' $false $_.Exception.Message
}

# -- 7. Single DB / single secret ----------------------------─
Section 'DB & secret consistency'
$appdataDb = "$env:APPDATA\CommClient\data\commclient.db"
$dbInstances = @()
if (Test-Path $appdataDb) { $dbInstances += $appdataDb }
$candidates = @(
    'C:\Users\youse\c\wifi\CommClient-Server\data\commclient.db',
    'C:\Users\youse\c\wifi\CommClient-Server\dist\Helen-Server\_internal\data\commclient.db',
    'C:\Users\youse\c\wifi\CommClient-Desktop\release\win-unpacked\resources\server\_internal\data\commclient.db'
)
foreach ($c in $candidates) {
    if (Test-Path $c) {
        $size = (Get-Item $c).Length
        if ($size -gt 4096) { $dbInstances += $c }
    }
}
$dbDetail = if ($dbInstances.Count -gt 1) {
    "found $($dbInstances.Count) DBs -- split data risk:`n         " + ($dbInstances -join "`n         ")
} else { $appdataDb }
Write-Check 'Single active commclient.db' ($dbInstances.Count -le 1) $dbDetail

$secretsFile = "$env:APPDATA\CommClient\data\.secrets.json"
if (Test-Path $secretsFile) {
    try {
        $sec = Get-Content -Raw $secretsFile | ConvertFrom-Json
        $jwtPersisted = $sec.jwt_secret
        Write-Check '.secrets.json present (persistent JWT)' $true (
            "jwt_secret length=$($jwtPersisted.Length) chars"
        )

        $envFiles = @(
            'C:\Users\youse\c\wifi\CommClient-Server\.env',
            'C:\Users\youse\c\wifi\CommClient-Server\dist\Helen-Server\.env'
        )
        foreach ($e in $envFiles) {
            if (Test-Path $e) {
                $line = (Select-String -Path $e -Pattern '^JWT_SECRET=(.+)$' | Select-Object -First 1)
                if ($line) {
                    $envSecret = $line.Matches[0].Groups[1].Value
                    $match = $envSecret -eq $jwtPersisted
                    $jwtDetail = if ($match) { 'in sync' } else { 'MISMATCH -- tokens issued by one server will be rejected by the other' }
                    Write-Check ".env JWT matches .secrets.json ($e)" $match $jwtDetail
                } else {
                    Write-Check ".env JWT line ($e)" $false 'JWT_SECRET line not found'
                }
            }
        }
    } catch {
        Write-Check '.secrets.json parse' $false $_.Exception.Message
    }
} else {
    Write-Check '.secrets.json present (persistent JWT)' $false (
        "missing: $secretsFile -- server will generate a new JWT_SECRET on every restart, invalidating tokens"
    )
}

# -- Summary --------------------------------------------------
Write-Host ''
if ($Script:Failed -eq 0) {
    Write-Host 'All checks passed.' -ForegroundColor Green
    exit 0
} else {
    Write-Host "$Script:Failed check(s) failed." -ForegroundColor Red
    exit 1
}
