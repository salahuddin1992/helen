#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Uninstall CommClient-Server Windows service

.DESCRIPTION
    This script safely removes the CommClient-Server Windows service:
    - Stops the service if running
    - Removes the service registration
    - Cleans up NSSM configuration

.PARAMETER ServiceName
    Name of the service to remove (default: CommClientServer)

.PARAMETER NSSMPath
    Path to NSSM executable. If not provided, searches in PATH.

.EXAMPLE
    PS> .\uninstall-service.ps1
    # Removes the default CommClientServer service

.EXAMPLE
    PS> .\uninstall-service.ps1 -ServiceName "MyService"
    # Removes a custom named service
#>

param(
    [string]$ServiceName = "CommClientServer",
    [string]$NSSMPath = ""
)

# ============================================================================
# Configuration
# ============================================================================
$ErrorActionPreference = "Stop"
$VerbosePreference = "Continue"

# Script location
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$NSSM_LOCAL_DIR = Join-Path $ProjectRoot "bin\nssm"

# ============================================================================
# Helper Functions
# ============================================================================

function Write-Status {
    param([string]$Message, [string]$Type = "Info")
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $prefix = switch ($Type) {
        "Success" { "[✓]" }
        "Error" { "[✗]" }
        "Warning" { "[!]" }
        "Info" { "[*]" }
        default { "[*]" }
    }
    $color = switch ($Type) {
        "Success" { "Green" }
        "Error" { "Red" }
        "Warning" { "Yellow" }
        default { "Cyan" }
    }
    Write-Host "$prefix $Message" -ForegroundColor $color
}

function Test-AdminPrivileges {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Find-NSSM {
    Write-Status "Locating NSSM..." "Info"

    # Check if provided NSSMPath exists
    if ($NSSMPath -and (Test-Path $NSSMPath)) {
        Write-Status "Found NSSM at: $NSSMPath" "Success"
        return $NSSMPath
    }

    # Check in PATH
    try {
        $nssmCmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
        if ($nssmCmd) {
            $nssmPath = $nssmCmd.Source
            Write-Status "Found NSSM in PATH: $nssmPath" "Success"
            return $nssmPath
        }
    }
    catch { }

    # Check local bin directory
    $localNssm = Join-Path $NSSM_LOCAL_DIR "nssm.exe"
    if (Test-Path $localNssm) {
        Write-Status "Found NSSM locally: $localNssm" "Success"
        return $localNssm
    }

    Write-Status "NSSM not found in PATH or project directory" "Error"
    Write-Status "Please ensure NSSM is installed and in your PATH" "Error"
    return $null
}

function Test-ServiceExists {
    param([string]$Name)
    try {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        return $null -ne $service
    }
    catch {
        return $false
    }
}

function Get-UserConfirmation {
    param(
        [string]$Message,
        [string]$DefaultChoice = "N"
    )

    $choices = @("Y", "N")
    $default = if ($DefaultChoice -eq "Y") { 0 } else { 1 }

    $decision = $Host.UI.PromptForChoice("", $Message, $choices, $default)
    return $decision -eq 0
}

function Stop-ServiceGracefully {
    param([string]$Name)

    Write-Status "Checking service status..." "Info"

    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $service) {
        Write-Status "Service not found: $Name" "Warning"
        return $true
    }

    if ($service.Status -eq "Stopped") {
        Write-Status "Service is already stopped" "Info"
        return $true
    }

    Write-Status "Stopping service '$Name'..." "Info"
    try {
        Stop-Service -Name $Name -Force -ErrorAction Stop -WarningAction SilentlyContinue
        # Wait for service to stop
        $timeout = 0
        while ((Get-Service -Name $Name).Status -ne "Stopped" -and $timeout -lt 10) {
            Start-Sleep -Milliseconds 500
            $timeout++
        }

        if ((Get-Service -Name $Name).Status -eq "Stopped") {
            Write-Status "Service stopped successfully" "Success"
            Start-Sleep -Milliseconds 500
            return $true
        }
        else {
            Write-Status "Service did not stop within timeout" "Error"
            return $false
        }
    }
    catch {
        Write-Status "Error stopping service: $_" "Error"
        return $false
    }
}

function Remove-Service {
    param(
        [string]$Name,
        [string]$NssmPath
    )

    Write-Status "Removing service '$Name'..." "Info"

    try {
        # Remove service via NSSM
        Write-Status "Executing NSSM remove command..." "Info"
        $output = & $NssmPath remove $Name confirm 2>&1
        Write-Verbose "NSSM output: $output"

        # Wait for registry to update
        Start-Sleep -Milliseconds 1000

        # Verify service is removed
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($null -eq $service) {
            Write-Status "Service removed successfully" "Success"
            return $true
        }
        else {
            Write-Status "Service still exists after removal attempt" "Warning"
            return $false
        }
    }
    catch {
        Write-Status "Error removing service: $_" "Error"
        return $false
    }
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║  CommClient-Server Windows Service Uninstallation             ║" -ForegroundColor Cyan
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # Check admin rights
    if (-not (Test-AdminPrivileges)) {
        Write-Status "This script requires administrator privileges!" "Error"
        Write-Status "Please run PowerShell as Administrator and try again." "Error"
        exit 1
    }

    Write-Status "Service to remove: $ServiceName" "Info"
    Write-Host ""

    # Check if service exists
    if (-not (Test-ServiceExists $ServiceName)) {
        Write-Status "Service '$ServiceName' does not exist" "Warning"
        Write-Status "Nothing to uninstall." "Info"
        exit 0
    }

    # Confirmation
    $message = "Are you sure you want to remove the '$ServiceName' service? This action cannot be undone."
    if (-not (Get-UserConfirmation $message)) {
        Write-Status "Uninstallation cancelled by user" "Info"
        exit 0
    }

    # Find NSSM
    $nssmPath = Find-NSSM
    if (-not $nssmPath) {
        Write-Status "Cannot proceed without NSSM" "Error"
        exit 1
    }

    # Stop service gracefully
    if (-not (Stop-ServiceGracefully $ServiceName)) {
        $message = "Failed to stop service. Continue with removal anyway?"
        if (-not (Get-UserConfirmation $message "N")) {
            Write-Status "Uninstallation cancelled" "Info"
            exit 1
        }
    }

    # Remove service
    if (-not (Remove-Service $ServiceName $nssmPath)) {
        Write-Status "Service removal encountered errors (see above)" "Error"
        exit 1
    }

    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║  Uninstallation Complete!                                      ║" -ForegroundColor Green
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
    Write-Status "Service '$ServiceName' has been removed" "Success"
    Write-Host ""
    Write-Host "Additional cleanup (optional):" -ForegroundColor Cyan
    Write-Host "  - Remove logs: Remove-Item $(Join-Path $ProjectRoot 'data\logs\*') -Recurse"
    Write-Host "  - Remove venv: Remove-Item $(Join-Path $ProjectRoot 'venv') -Recurse"
    Write-Host ""
}

Main
