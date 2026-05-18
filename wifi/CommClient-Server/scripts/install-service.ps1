#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install CommClient-Server as a Windows service using NSSM (Non-Sucking Service Manager)

.DESCRIPTION
    This script installs the CommClient-Server Python backend as a Windows service that:
    - Starts automatically on system boot
    - Restarts automatically on failure
    - Runs with proper environment variables from .env file
    - Manages logging and lifecycle through NSSM

.PARAMETER NSSMPath
    Path to NSSM executable. If not provided, attempts to locate or download it.

.PARAMETER ServiceName
    Name of the Windows service (default: CommClientServer)

.PARAMETER ProjectRoot
    Root directory of CommClient-Server project (default: parent of scripts directory)

.EXAMPLE
    PS> .\install-service.ps1
    # Uses defaults, searches PATH for NSSM, installs as CommClientServer service

.EXAMPLE
    PS> .\install-service.ps1 -NSSMPath "C:\nssm\nssm.exe" -ServiceName "MyCommClient"
    # Uses specific NSSM binary and custom service name
#>

param(
    [string]$NSSMPath = "",
    [string]$ServiceName = "CommClientServer",
    [string]$ProjectRoot = ""
)

# ============================================================================
# Configuration & Constants
# ============================================================================
$ErrorActionPreference = "Stop"
$VerbosePreference = "Continue"

# Script location and project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $ScriptDir
}

$EnvFile = Join-Path $ProjectRoot ".env"
$VenvDir = Join-Path $ProjectRoot "venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$RunScript = Join-Path $ProjectRoot "run.py"
$LogDir = Join-Path $ProjectRoot "data\logs"

$NSSM_DOWNLOAD_URL = "https://nssm.cc/download/nssm-2.24-101-g897c7f7.zip"
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
    Write-Status "Searching for NSSM..." "Info"

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

    return $null
}

function Install-NSSM {
    Write-Status "NSSM not found. Attempting installation..." "Warning"

    # Try winget first (preferred for modern Windows)
    try {
        Write-Status "Trying to install NSSM via WinGet..." "Info"
        winget install nssm -y -e 2>&1 | Out-Null

        $nssmCmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
        if ($nssmCmd) {
            Write-Status "Successfully installed NSSM via WinGet" "Success"
            return $nssmCmd.Source
        }
    }
    catch {
        Write-Status "WinGet installation failed, trying direct download..." "Warning"
    }

    # Manual download and setup
    try {
        Write-Status "Downloading NSSM from official source..." "Info"

        # Create bin directory
        if (-not (Test-Path $NSSM_LOCAL_DIR)) {
            New-Item -ItemType Directory -Force -Path $NSSM_LOCAL_DIR | Out-Null
        }

        $tempZip = Join-Path $env:TEMP "nssm.zip"

        # Download with progress
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $NSSM_DOWNLOAD_URL -OutFile $tempZip -TimeoutSec 30
        $ProgressPreference = 'Continue'

        Write-Status "Extracting NSSM..." "Info"
        Expand-Archive -Path $tempZip -DestinationPath $NSSM_LOCAL_DIR -Force
        Remove-Item $tempZip -Force

        # Find nssm.exe in extracted directory
        $nssmExe = Get-ChildItem -Path $NSSM_LOCAL_DIR -Filter "nssm.exe" -Recurse | Select-Object -First 1
        if ($nssmExe) {
            Write-Status "Successfully installed NSSM to: $($nssmExe.FullName)" "Success"
            return $nssmExe.FullName
        }
        else {
            Write-Status "NSSM executable not found in archive" "Error"
            return $null
        }
    }
    catch {
        Write-Status "Failed to download NSSM: $_" "Error"
        Write-Status "Please manually download from https://nssm.cc/download and place nssm.exe in PATH" "Warning"
        return $null
    }
}

function Test-ProjectStructure {
    Write-Status "Validating project structure..." "Info"

    $checks = @(
        @{ Path = $ProjectRoot; Name = "Project root" }
        @{ Path = $EnvFile; Name = ".env configuration file" }
        @{ Path = $RunScript; Name = "run.py launcher" }
    )

    foreach ($check in $checks) {
        if (Test-Path $check.Path) {
            Write-Status "  ✓ Found $($check.Name): $($check.Path)" "Success"
        }
        else {
            Write-Status "  ✗ Missing $($check.Name): $($check.Path)" "Error"
            return $false
        }
    }

    return $true
}

function Test-PythonEnvironment {
    Write-Status "Checking Python virtual environment..." "Info"

    # Check if venv exists
    if (-not (Test-Path $VenvDir)) {
        Write-Status "  Creating virtual environment..." "Info"
        try {
            & python.exe -m venv $VenvDir
            Write-Status "  ✓ Virtual environment created" "Success"
        }
        catch {
            Write-Status "  ✗ Failed to create venv: $_" "Error"
            return $false
        }
    }
    else {
        Write-Status "  ✓ Virtual environment exists" "Success"
    }

    # Check if python.exe exists in venv
    if (-not (Test-Path $PythonExe)) {
        Write-Status "  ✗ Python executable not found in venv: $PythonExe" "Error"
        return $false
    }

    Write-Status "  ✓ Python executable: $PythonExe" "Success"
    return $true
}

function Install-Dependencies {
    Write-Status "Installing/updating Python dependencies..." "Info"

    $reqFile = Join-Path $ProjectRoot "requirements.txt"
    if (-not (Test-Path $reqFile)) {
        Write-Status "  ✗ requirements.txt not found" "Error"
        return $false
    }

    try {
        $output = & $PythonExe -m pip install -r $reqFile 2>&1
        Write-Status "  ✓ Dependencies installed successfully" "Success"
        return $true
    }
    catch {
        Write-Status "  ✗ Failed to install dependencies: $_" "Error"
        return $false
    }
}

function Read-EnvFile {
    Write-Status "Reading environment variables from .env..." "Info"

    $envVars = @{}

    if (-not (Test-Path $EnvFile)) {
        Write-Status "  ! .env file not found, using defaults" "Warning"
        return $envVars
    }

    Get-Content $EnvFile | Where-Object { $_ -match '^[^#]' -and $_.Trim() } | ForEach-Object {
        $line = $_.Trim()
        if ($line -match '^\s*([^=]+)=(.*)$') {
            $key = $matches[1].Trim()
            $value = $matches[2].Trim()
            if ($key) {
                $envVars[$key] = $value
                Write-Verbose "  Loaded: $key = $(if ($key -match 'secret|password|token') { '***' } else { $value })"
            }
        }
    }

    Write-Status "  ✓ Loaded $($envVars.Count) environment variables" "Success"
    return $envVars
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

function Remove-ExistingService {
    param([string]$Name, [string]$NssmPath)

    Write-Status "Service '$Name' already exists. Removing..." "Warning"

    try {
        # Stop service if running
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($service) {
            if ($service.Status -eq "Running") {
                Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 500
            }

            # Remove via NSSM
            & $NssmPath remove $Name confirm 2>&1 | Out-Null
            Start-Sleep -Milliseconds 1000
            Write-Status "  ✓ Existing service removed" "Success"
        }
    }
    catch {
        Write-Status "  ! Error removing existing service: $_" "Warning"
    }
}

function Create-LogDirectory {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        Write-Status "Created log directory: $LogDir" "Info"
    }
}

function Install-Service {
    param(
        [string]$Name,
        [string]$NssmPath,
        [hashtable]$EnvVars
    )

    Write-Status "Installing Windows service '$Name'..." "Info"

    # Build NSSM install command
    $nssm = $NssmPath
    $app = $PythonExe
    $appArgs = $RunScript
    $appDir = $ProjectRoot

    Write-Status "  Service details:" "Info"
    Write-Status "    Name:           $Name" "Info"
    Write-Status "    Python:         $app" "Info"
    Write-Status "    Script:         $appArgs" "Info"
    Write-Status "    Working Dir:    $appDir" "Info"

    try {
        # Install service
        Write-Status "  Installing service with NSSM..." "Info"
        & $nssm install $Name $app $appArgs 2>&1 | Out-Null

        # Set working directory
        & $nssm set $Name AppDirectory $appDir 2>&1 | Out-Null

        # Configure startup behavior
        & $nssm set $Name Start SERVICE_AUTO_START 2>&1 | Out-Null
        & $nssm set $Name Type SERVICE_WIN32_OWN_PROCESS 2>&1 | Out-Null

        # Configure restart on failure
        & $nssm set $Name AppRestartDelay 5000 2>&1 | Out-Null
        & $nssm set $Name AppExit Default Restart 2>&1 | Out-Null

        # Configure logging
        $logFile = Join-Path $LogDir "service.log"
        & $nssm set $Name AppStdout $logFile 2>&1 | Out-Null
        & $nssm set $Name AppStderr $logFile 2>&1 | Out-Null
        & $nssm set $Name AppStdoutCreationDisposition 4 2>&1 | Out-Null  # Append
        & $nssm set $Name AppStderrCreationDisposition 4 2>&1 | Out-Null

        # Set environment variables
        Write-Status "  Setting environment variables..." "Info"
        foreach ($key in $EnvVars.Keys) {
            $value = $EnvVars[$key]
            & $nssm set $Name AppEnvironmentExtra "$key=$value" 2>&1 | Out-Null
        }

        Write-Status "  ✓ Service installed successfully" "Success"
        return $true
    }
    catch {
        Write-Status "  ✗ Failed to install service: $_" "Error"
        return $false
    }
}

function Configure-Firewall {
    Write-Status "Configuring Windows Firewall (optional)..." "Info"

    try {
        # This is optional and informational
        Write-Status "  Note: Run scripts\setup-firewall.ps1 to open firewall ports" "Info"
    }
    catch {
        Write-Status "  Could not verify firewall settings" "Warning"
    }
}

function Verify-ServiceInstallation {
    param([string]$Name)

    Write-Status "Verifying service installation..." "Info"

    try {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($service) {
            Write-Status "  ✓ Service found: $($service.Name)" "Success"
            Write-Status "    Status:      $($service.Status)" "Info"
            Write-Status "    StartType:   $($service.StartType)" "Info"
            return $true
        }
        else {
            Write-Status "  ✗ Service not found" "Error"
            return $false
        }
    }
    catch {
        Write-Status "  ✗ Error verifying service: $_" "Error"
        return $false
    }
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║  CommClient-Server Windows Service Installation (NSSM)         ║" -ForegroundColor Cyan
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # Check admin rights
    if (-not (Test-AdminPrivileges)) {
        Write-Status "This script requires administrator privileges!" "Error"
        Write-Status "Please run PowerShell as Administrator and try again." "Error"
        exit 1
    }

    # Validate project structure
    if (-not (Test-ProjectStructure)) {
        Write-Status "Project structure validation failed!" "Error"
        exit 1
    }

    # Check and setup Python environment
    if (-not (Test-PythonEnvironment)) {
        Write-Status "Python environment validation failed!" "Error"
        exit 1
    }

    # Install dependencies
    if (-not (Install-Dependencies)) {
        Write-Status "Dependency installation failed!" "Error"
        exit 1
    }

    # Find or install NSSM
    $nssmPath = Find-NSSM
    if (-not $nssmPath) {
        $nssmPath = Install-NSSM
        if (-not $nssmPath) {
            Write-Status "NSSM installation failed. Aborting." "Error"
            exit 1
        }
    }

    # Create log directory
    Create-LogDirectory

    # Read environment variables
    $envVars = Read-EnvFile

    # Check if service already exists
    if (Test-ServiceExists $ServiceName) {
        Remove-ExistingService $ServiceName $nssmPath
    }

    # Install the service
    if (-not (Install-Service $ServiceName $nssmPath $envVars)) {
        Write-Status "Service installation failed!" "Error"
        exit 1
    }

    # Verify installation
    if (-not (Verify-ServiceInstallation $ServiceName)) {
        Write-Status "Service installation verification failed!" "Error"
        exit 1
    }

    # Optional firewall configuration
    Configure-Firewall

    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║  Installation Complete!                                        ║" -ForegroundColor Green
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
    Write-Status "Service Name: $ServiceName" "Success"
    Write-Status "Status: Ready to start" "Success"
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Cyan
    Write-Host "  1. Configure firewall (if needed):"
    Write-Host "     PS> .\setup-firewall.ps1"
    Write-Host ""
    Write-Host "  2. Start the service:"
    Write-Host "     PS> Start-Service $ServiceName"
    Write-Host ""
    Write-Host "  3. View service status:"
    Write-Host "     PS> Get-Service $ServiceName"
    Write-Host ""
    Write-Host "  4. View service logs:"
    Write-Host "     PS> Get-Content $(Join-Path $LogDir 'service.log') -Tail 50 -Wait"
    Write-Host ""
}

Main
