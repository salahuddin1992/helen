<#
.SYNOPSIS
    Helen deployment end-to-end health check (Windows).

.DESCRIPTION
    Mirror of health-check.sh. Verifies endpoint, rendezvous, ports,
    services, data freshness, JWT_SECRET strength.

.EXAMPLE
    PS> .\health-check.ps1
    PS> .\health-check.ps1 -ServerUrl http://10.0.0.5:3000
#>
param(
    [string]$ServerUrl     = "http://localhost:3000",
    [string]$RendezvousUrl = "http://localhost:9090",
    [string]$DataDir       = "C:\Program Files\Helen-Server\_internal\data",
    [int]   $WarnDiskPct   = 85
)

$pass = 0; $fail = 0; $warn = 0
function Green($m) { Write-Host "  [+] $m" -ForegroundColor Green; $script:pass++ }
function Red  ($m) { Write-Host "  [-] $m" -ForegroundColor Red;   $script:fail++ }
function Yellow($m){ Write-Host "  [!] $m" -ForegroundColor Yellow; $script:warn++ }
function Section($t){ Write-Host "`n-- $t --" -ForegroundColor Cyan }

# 1. Server
Section "Helen-Server"
try {
    $r = Invoke-RestMethod -Uri "$ServerUrl/api/health" -TimeoutSec 5 -ErrorAction Stop
    if ($r.status -eq "ok") {
        Green "endpoint $ServerUrl/api/health (status=ok, version=$($r.version))"
    } else {
        Red "endpoint reachable, body unexpected: $r"
    }
} catch { Red "endpoint $ServerUrl/api/health unreachable: $_" }

# 2. Rendezvous
Section "Helen-Rendezvous"
try {
    $null = Invoke-WebRequest -Uri "$RendezvousUrl/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Green "rendezvous $RendezvousUrl/health responding"
} catch {
    Yellow "rendezvous unreachable (only required on hosts running Rendezvous)"
}

# 3. Open ports
Section "Listening ports"
$ports = @(
    @{Port=3000; Label="Helen-Server HTTP"},
    @{Port=3443; Label="Helen-Server HTTPS"},
    @{Port=41234; Label="Helen UDP discovery"; Udp=$true},
    @{Port=5353; Label="mDNS"; Udp=$true}
)
foreach ($p in $ports) {
    $found = if ($p.Udp) {
        Get-NetUDPEndpoint -LocalPort $p.Port -ErrorAction SilentlyContinue
    } else {
        Get-NetTCPConnection -LocalPort $p.Port -State Listen -ErrorAction SilentlyContinue
    }
    if ($found) { Green "$($p.Label) on port $($p.Port)" }
    else        { Yellow "$($p.Label) on port $($p.Port) - not listening" }
}

# 4. Windows services
Section "Windows services"
foreach ($svc in @("HelenServer","HelenRendezvous")) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        if ($s.Status -eq "Running") { Green "${svc}: $($s.Status)" }
        else                          { Red "${svc}: $($s.Status)" }
    } else {
        Yellow "${svc}: not installed"
    }
}

# 5. Data dir
Section "Data integrity"
if (Test-Path $DataDir) {
    Green "data dir present: $DataDir"
    $db = Join-Path $DataDir "commclient.db"
    if (Test-Path $db) {
        $age = ((Get-Date) - (Get-Item $db).LastWriteTime).TotalMinutes
        if ($age -lt 60)        { Green "commclient.db updated $([int]$age) min ago" }
        elseif ($age -lt 1440)  { Yellow "commclient.db last touched $([int]$age) min ago" }
        else                    { Yellow ("commclient.db idle for {0:N1} hours" -f ($age/60)) }
    } else { Yellow "commclient.db not found (server hasn't initialised data yet)" }

    $drive = (Get-Item $DataDir).PSDrive
    $pct = [int](100 - ($drive.Free / ($drive.Used + $drive.Free) * 100))
    if ($pct -gt $WarnDiskPct) { Red "disk $pct% full ($($drive.Name): drive)" }
    else                       { Green "disk $pct% used on $($drive.Name): drive" }
} else { Yellow "data dir not present locally: $DataDir" }

# 6. JWT secret
Section "Secrets"
$envFile = Join-Path (Split-Path $DataDir -Parent | Split-Path -Parent) ".env"
if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^JWT_SECRET=' } | Select-Object -First 1
    $val  = $line -replace '^JWT_SECRET=',''
    if (-not $val)                      { Red "JWT_SECRET missing in $envFile" }
    elseif ($val.Length -lt 32)         { Red "JWT_SECRET too short ($($val.Length) chars)" }
    elseif ($val -match '(?i)change|placeholder|todo') {
                                          Red "JWT_SECRET looks like a placeholder" }
    else                                { Green "JWT_SECRET length: $($val.Length) chars" }
} else { Yellow ".env not found at $envFile" }

# 7. Signature check
Section "Code signing"
foreach ($exe in @(
    (Join-Path (Split-Path $DataDir -Parent | Split-Path -Parent) "Helen-Server.exe")
)) {
    if (Test-Path $exe) {
        $s = Get-AuthenticodeSignature $exe
        switch ($s.Status) {
            "Valid"        { Green "$([System.IO.Path]::GetFileName($exe)) signed (Trusted)" }
            "UnknownError" { Yellow "$([System.IO.Path]::GetFileName($exe)) self-signed (cert not in TrustedRoot, run with -ImportToTrustedRoot)" }
            default        { Red "$([System.IO.Path]::GetFileName($exe)) signature: $($s.Status)" }
        }
    }
}

# Summary
Write-Host
Write-Host "============================================="
Write-Host ("  Result: {0} passed" -f $pass) -ForegroundColor Green -NoNewline
if ($warn -gt 0) { Write-Host ("  {0} warnings" -f $warn) -ForegroundColor Yellow -NoNewline }
if ($fail -gt 0) { Write-Host ("  {0} failures" -f $fail) -ForegroundColor Red -NoNewline }
Write-Host
Write-Host "============================================="

if ($fail -gt 0) { exit 1 }
