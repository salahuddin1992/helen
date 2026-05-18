# =============================================================================
#  start-lan-server.ps1 — One-command LAN-server launcher (Task #6)
# =============================================================================
#
#  Brings up the CommClient backend in the "PC = LAN server" topology:
#
#    1. Resolves the server's primary private-LAN IP (physical adapter only).
#    2. Opens Windows Firewall inbound rules for:
#         - HTTP   (default 3000)
#         - UDP discovery broadcast (default 41234)
#         - SFU control API (default 4443, loopback-only)
#         - mediasoup RTC UDP range (default 40000-49999)
#         - STUN/TURN (3478 udp+tcp, 5349 tcp)
#    3. Installs sfu-worker npm deps if node_modules is missing.
#    4. Creates/activates the Python venv and installs requirements.
#    5. Launches uvicorn on `app.lan_server_app:app` — the ASGI entry that
#       wires extended_bootstrap (persistent JWT secret + SFU launcher +
#       LAN CORS) into the existing FastAPI lifespan.
#    6. Prints a big banner with the LAN URL that clients should configure.
#
#  Usage (from an Admin PowerShell prompt):
#    powershell -ExecutionPolicy Bypass -File .\scripts\start-lan-server.ps1
#
#  Parameters:
#    -Port     HTTP port (default 3000)
#    -NoFirewall    Skip firewall rule creation (for CI / already-configured hosts)
#    -SkipSfuInstall  Don't run `npm install` in sfu-worker
#    -NoSfu    Disable SFU auto-launch (mesh-only mode)
#
#  Env output:
#    - HOST=0.0.0.0
#    - PORT=$Port
#    - COMMCLIENT_DATA_DIR=$env:APPDATA\CommClient\data
#    - ICE_ANNOUNCED_IP=<primary LAN IP>
#    - MEDIASOUP_ANNOUNCED_IP=<primary LAN IP>
# =============================================================================

[CmdletBinding()]
param(
    [int]    $Port            = 3000,
    [int]    $DiscoveryPort   = 41234,
    [int]    $SfuControlPort  = 4443,
    [int]    $RtcMinPort      = 40000,
    [int]    $RtcMaxPort      = 49999,
    [int]    $StunPort        = 3478,
    [int]    $TurnTlsPort     = 5349,
    [switch] $NoFirewall,
    [switch] $SkipSfuInstall,
    [switch] $NoSfu,
    [switch] $NoVenv
)

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# ── Paths ───────────────────────────────────────────────────────────────────
$ScriptRoot    = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerRoot    = Split-Path -Parent $ScriptRoot
$SfuWorkerRoot = Join-Path $ServerRoot 'sfu-worker'
$VenvRoot      = Join-Path $ServerRoot 'venv'
$DataDir       = Join-Path $env:APPDATA 'CommClient\data'
$LogsDir       = Join-Path $env:APPDATA 'CommClient\logs'

function Write-Section($text) {
    Write-Host ''
    Write-Host ('=' * 70) -ForegroundColor Cyan
    Write-Host ('  ' + $text)   -ForegroundColor Cyan
    Write-Host ('=' * 70) -ForegroundColor Cyan
}

function Write-Info($text)  { Write-Host "  [info]    $text"  -ForegroundColor Gray  }
function Write-Ok($text)    { Write-Host "  [ok]      $text"  -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [warn]    $text"  -ForegroundColor Yellow }
function Write-Err($text)   { Write-Host "  [error]   $text"  -ForegroundColor Red   }

# ── 1. Detect primary LAN IP ────────────────────────────────────────────────
Write-Section '1/6  Detecting primary LAN IP'

function Get-PrimaryLanIp {
    $denylist = @('vEthernet', 'VirtualBox', 'VMware', 'Hyper-V', 'WSL',
                  'Bluetooth', 'Loopback', 'Teredo', 'isatap', 'Tailscale',
                  'ZeroTier', 'WireGuard', 'OpenVPN')

    $candidates = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.AddressState -eq 'Preferred' -and
            $_.PrefixOrigin -ne 'WellKnown'
        }

    $physical = $candidates | Where-Object {
        $ifName = (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue).Name
        $keep = $true
        foreach ($h in $denylist) {
            if ($ifName -and ($ifName -like "*$h*")) { $keep = $false; break }
        }
        $keep
    }

    if ($physical.Count -gt 0) { return $physical[0].IPAddress }
    if ($candidates.Count -gt 0) { return $candidates[0].IPAddress }
    return '127.0.0.1'
}

$PrimaryIp = Get-PrimaryLanIp
Write-Ok  "Primary LAN IP: $PrimaryIp"
Write-Info "Data dir:        $DataDir"
Write-Info "Logs dir:        $LogsDir"

if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null }

# ── 2. Firewall rules ───────────────────────────────────────────────────────
if (-not $NoFirewall) {
    Write-Section '2/6  Configuring Windows Firewall'

    function Ensure-FirewallRule {
        param(
            [string]$Name,
            [string]$Protocol,
            [string]$LocalPort,
            [string]$Direction = 'Inbound'
        )
        try {
            $existing = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
            if ($existing) {
                Write-Info "$Name (already exists)"
                return
            }
            New-NetFirewallRule `
                -DisplayName $Name `
                -Direction $Direction `
                -Action Allow `
                -Protocol $Protocol `
                -LocalPort $LocalPort `
                -Profile Any `
                -Enabled True | Out-Null
            Write-Ok "$Name"
        } catch {
            Write-Warn2 "Could not create $Name : $($_.Exception.Message)"
        }
    }

    Ensure-FirewallRule -Name 'CommClient HTTP (TCP)'          -Protocol TCP -LocalPort $Port
    Ensure-FirewallRule -Name 'CommClient Discovery (UDP)'     -Protocol UDP -LocalPort $DiscoveryPort
    Ensure-FirewallRule -Name 'CommClient mDNS (UDP 5353)'     -Protocol UDP -LocalPort 5353
    Ensure-FirewallRule -Name 'CommClient SFU Control (TCP)'   -Protocol TCP -LocalPort $SfuControlPort
    Ensure-FirewallRule -Name 'CommClient STUN/TURN (UDP)'     -Protocol UDP -LocalPort $StunPort
    Ensure-FirewallRule -Name 'CommClient STUN/TURN (TCP)'     -Protocol TCP -LocalPort $StunPort
    Ensure-FirewallRule -Name 'CommClient TURN TLS (TCP)'      -Protocol TCP -LocalPort $TurnTlsPort
    Ensure-FirewallRule -Name 'CommClient mediasoup RTC (UDP)' -Protocol UDP -LocalPort "$RtcMinPort-$RtcMaxPort"
    Ensure-FirewallRule -Name 'CommClient mediasoup RTC (TCP)' -Protocol TCP -LocalPort "$RtcMinPort-$RtcMaxPort"
} else {
    Write-Section '2/6  Skipping firewall setup (-NoFirewall)'
}

# ── 3. SFU worker dependencies ──────────────────────────────────────────────
if ($NoSfu) {
    Write-Section '3/6  Skipping SFU worker (-NoSfu)'
} elseif ($SkipSfuInstall) {
    Write-Section '3/6  Skipping SFU npm install (-SkipSfuInstall)'
} else {
    Write-Section '3/6  Installing SFU worker dependencies'

    if (-not (Test-Path (Join-Path $SfuWorkerRoot 'package.json'))) {
        Write-Warn2 "sfu-worker directory missing: $SfuWorkerRoot"
    } else {
        Push-Location $SfuWorkerRoot
        try {
            if (-not (Test-Path 'node_modules\mediasoup\package.json')) {
                $npmCmd = (Get-Command npm -ErrorAction SilentlyContinue).Source
                if (-not $npmCmd) {
                    Write-Err 'npm not on PATH — install Node.js >=18.19 from https://nodejs.org/'
                } else {
                    Write-Info 'Running npm install (mediasoup compiles native bindings, takes 1-3 minutes)...'
                    & npm install --omit=dev --no-audit --no-fund
                    if ($LASTEXITCODE -ne 0) {
                        Write-Err "npm install failed (exit $LASTEXITCODE)"
                    } else {
                        Write-Ok 'SFU worker deps installed'
                    }
                }
            } else {
                Write-Ok 'SFU worker deps already present'
            }
        } finally {
            Pop-Location
        }
    }
}

# ── 4. Python venv + deps ──────────────────────────────────────────────────
if ($NoVenv) {
    Write-Section '4/6  Skipping venv creation (-NoVenv) — using system Python'
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $PythonExe) { $PythonExe = (Get-Command py -ErrorAction SilentlyContinue).Source }
} else {
    Write-Section '4/6  Preparing Python venv'

    $PythonExe = Join-Path $VenvRoot 'Scripts\python.exe'
    if (-not (Test-Path $PythonExe)) {
        $SystemPy = (Get-Command py -ErrorAction SilentlyContinue).Source
        if (-not $SystemPy) { $SystemPy = (Get-Command python -ErrorAction SilentlyContinue).Source }
        if (-not $SystemPy) {
            Write-Err 'python not on PATH — install Python 3.11+ from https://python.org'
            exit 2
        }
        Write-Info "Creating venv at $VenvRoot"
        & $SystemPy -3 -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) {
            # Fallback for distros where `py -3` doesn't exist
            & $SystemPy -m venv $VenvRoot
        }
    }

    if (Test-Path (Join-Path $ServerRoot 'requirements.txt')) {
        & $PythonExe -m pip install --upgrade pip --quiet
        Write-Info 'Installing Python requirements (this may take a minute)...'
        & $PythonExe -m pip install -r (Join-Path $ServerRoot 'requirements.txt') --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 "pip install exited with $LASTEXITCODE — continuing anyway"
        } else {
            Write-Ok 'Python dependencies ready'
        }
    }
}

if (-not $PythonExe) {
    Write-Err 'No Python executable resolved — aborting'
    exit 3
}

# ── 5. Start the server ────────────────────────────────────────────────────
Write-Section '5/6  Launching CommClient LAN server'

$env:HOST                       = '0.0.0.0'
$env:PORT                       = "$Port"
$env:COMMCLIENT_DATA_DIR        = $DataDir
$env:LOG_DIR                    = $LogsDir
$env:SQLITE_PATH                = (Join-Path $DataDir 'commclient.db')
$env:UPLOAD_DIR                 = (Join-Path $DataDir 'files')
$env:ICE_ANNOUNCED_IP           = $PrimaryIp
$env:MEDIASOUP_ANNOUNCED_IP     = $PrimaryIp
$env:MEDIASOUP_CONTROL_HOST     = '127.0.0.1'
$env:MEDIASOUP_CONTROL_PORT     = "$SfuControlPort"
$env:MEDIASOUP_MIN_PORT         = "$RtcMinPort"
$env:MEDIASOUP_MAX_PORT         = "$RtcMaxPort"

if ($NoSfu) { $env:COMMCLIENT_SFU_AUTOSTART_DISABLED = '1' }

Write-Info "Entry:      app.lan_server_app:app"
Write-Info "Bind:       0.0.0.0:$Port"
Write-Info "Announced:  $PrimaryIp"
Write-Info "Data dir:   $DataDir"

# Hand over to uvicorn — this process stays in the foreground so Ctrl+C
# gives a clean shutdown (FastAPI lifespan -> sfu_launcher.stop() -> etc.).
Push-Location $ServerRoot
try {
    & $PythonExe -m uvicorn 'app.lan_server_app:app' `
        --host '0.0.0.0' `
        --port $Port `
        --log-level info `
        --no-access-log
} finally {
    Pop-Location
}

# ── 6. Banner (only reached if uvicorn exits) ──────────────────────────────
Write-Section '6/6  Server exited'
Write-Info "If this was unexpected, check the logs at:"
Write-Info "  $LogsDir"
