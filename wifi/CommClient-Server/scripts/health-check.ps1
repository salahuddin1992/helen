<#
.SYNOPSIS
    Health check and auto-restart for CommClient-Server

.DESCRIPTION
    Monitors the CommClient-Server health by:
    - Checking HTTP GET /api/health endpoint
    - Attempting automatic restart on failure
    - Logging results to data/logs/health.log
    - Suitable for Windows scheduled tasks

.PARAMETER Endpoint
    Health check endpoint (default: http://localhost:3000/api/health)

.PARAMETER ServiceName
    Windows service name to restart if unhealthy (default: CommClientServer)

.PARAMETER TimeoutSeconds
    HTTP request timeout in seconds (default: 10)

.PARAMETER LogPath
    Log file path (default: ./data/logs/health.log)

.PARAMETER AutoRestart
    Automatically attempt restart if health check fails

.EXAMPLE
    PS> .\health-check.ps1
    # Checks health and logs results

.EXAMPLE
    PS> .\health-check.ps1 -AutoRestart
    # Checks health and restarts service if down

.NOTES
    Typical scheduled task: every 5 minutes
    powershell.exe -ExecutionPolicy Bypass -File ".\scripts\health-check.ps1" -AutoRestart
#>

param(
    [string]$Endpoint = "http://localhost:3000/api/health",
    [string]$ServiceName = "CommClientServer",
    [int]$TimeoutSeconds = 10,
    [string]$LogPath = "",
    [switch]$AutoRestart
)

# ============================================================================
# Configuration
# ============================================================================
$ErrorActionPreference = "Continue"

if (-not $LogPath) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $ProjectRoot = Split-Path -Parent $ScriptDir
    $LogPath = Join-Path $ProjectRoot "data\logs\health.log"
}

$LogDir = Split-Path -Parent $LogPath

# ============================================================================
# Helper Functions
# ============================================================================

function Write-Log {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )

    # Ensure log directory exists
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    }

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] [$Level] $Message"

    # Write to console
    $color = switch ($Level) {
        "ERROR" { "Red" }
        "WARN" { "Yellow" }
        "SUCCESS" { "Green" }
        default { "Cyan" }
    }
    Write-Host $logMessage -ForegroundColor $color

    # Append to log file
    Add-Content -Path $LogPath -Value $logMessage -ErrorAction SilentlyContinue
}

function Test-ServiceHealth {
    param([string]$Url)

    Write-Log "Checking health endpoint: $Url" "INFO"

    try {
        $response = Invoke-WebRequest `
            -Uri $Url `
            -Method Get `
            -TimeoutSec $TimeoutSeconds `
            -ErrorAction SilentlyContinue

        if ($response.StatusCode -eq 200) {
            Write-Log "Health check passed (HTTP 200)" "SUCCESS"
            return $true
        }
        else {
            Write-Log "Health check failed (HTTP $($response.StatusCode))" "ERROR"
            return $false
        }
    }
    catch {
        Write-Log "Health check failed: $($_.Exception.Message)" "ERROR"
        return $false
    }
}

function Get-ServiceStatus {
    param([string]$Name)

    try {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($service) {
            return $service.Status
        }
        else {
            return "NotFound"
        }
    }
    catch {
        return "Error"
    }
}

function Restart-Service {
    param([string]$Name)

    Write-Log "Attempting to restart service: $Name" "WARN"

    try {
        # Check if service exists
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if (-not $service) {
            Write-Log "Service not found: $Name" "ERROR"
            return $false
        }

        # Stop service
        Write-Log "Stopping service..." "INFO"
        Stop-Service -Name $Name -Force -ErrorAction Stop
        Start-Sleep -Seconds 2

        # Start service
        Write-Log "Starting service..." "INFO"
        Start-Service -Name $Name -ErrorAction Stop
        Start-Sleep -Seconds 3

        # Verify restart
        $status = Get-ServiceStatus $Name
        if ($status -eq "Running") {
            Write-Log "Service restarted successfully" "SUCCESS"
            return $true
        }
        else {
            Write-Log "Service failed to start (status: $status)" "ERROR"
            return $false
        }
    }
    catch {
        Write-Log "Error restarting service: $($_.Exception.Message)" "ERROR"
        return $false
    }
}

function Rotate-Logs {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    try {
        $file = Get-Item $Path
        if ($file.Length -gt 10MB) {
            Write-Log "Rotating log file (size: $('{0:N2}' -f ($file.Length / 1MB)) MB)" "INFO"

            $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $backupPath = "$Path.$timestamp"
            Rename-Item -Path $Path -NewName $backupPath -Force

            # Keep only last 5 rotated logs
            Get-ChildItem -Path $LogDir -Filter "health.log.*" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 | Remove-Item -Force
        }
    }
    catch {
        Write-Log "Error rotating logs: $_" "WARN"
    }
}

function Get-DetailedStatus {
    param([string]$ServiceName)

    Write-Log "Gathering diagnostic information..." "INFO"

    $status = Get-ServiceStatus $ServiceName
    Write-Log "  Service Status: $status" "INFO"

    if ($status -eq "Running") {
        try {
            $service = Get-Service -Name $ServiceName
            Write-Log "  Service Uptime: $($service.Status)" "INFO"
        }
        catch { }
    }

    # Check disk space
    try {
        $drive = Get-Item (Split-Path -Parent $LogPath)
        $diskFree = (Get-Volume -DriveLetter $drive.PSDrive.Name).SizeRemaining
        Write-Log "  Disk Free: $('{0:N2}' -f ($diskFree / 1GB)) GB" "INFO"
    }
    catch { }
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Log "═════════════════════════════════════════════════════════" "INFO"
    Write-Log "CommClient-Server Health Check" "INFO"
    Write-Log "═════════════════════════════════════════════════════════" "INFO"

    # Rotate logs if needed
    Rotate-Logs $LogPath

    # Check service health
    $isHealthy = Test-ServiceHealth $Endpoint

    if ($isHealthy) {
        Write-Log "Server is healthy and operational" "SUCCESS"
        Get-DetailedStatus $ServiceName
        Write-Log "═════════════════════════════════════════════════════════" "INFO"
        exit 0
    }
    else {
        Write-Log "Server health check failed" "ERROR"

        if ($AutoRestart) {
            Write-Log "AutoRestart enabled, attempting recovery..." "WARN"
            if (Restart-Service $ServiceName) {
                Start-Sleep -Seconds 5
                # Verify recovery
                if (Test-ServiceHealth $Endpoint) {
                    Write-Log "Server recovered successfully" "SUCCESS"
                    Write-Log "═════════════════════════════════════════════════════════" "INFO"
                    exit 0
                }
                else {
                    Write-Log "Server still unhealthy after restart" "ERROR"
                }
            }
            else {
                Write-Log "Service restart failed" "ERROR"
            }
        }

        Get-DetailedStatus $ServiceName
        Write-Log "═════════════════════════════════════════════════════════" "INFO"
        exit 1
    }
}

Main
