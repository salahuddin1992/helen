<#
.SYNOPSIS
    Start CommClient-Server in development mode

.DESCRIPTION
    This script starts the CommClient-Server development server with:
    - Python 3.10+ validation
    - Virtual environment creation/activation
    - Dependency installation
    - Database migrations
    - Graceful shutdown handling

.PARAMETER ProjectRoot
    Root directory of CommClient-Server project (default: parent of scripts directory)

.PARAMETER NoMigrations
    Skip database migrations on startup

.PARAMETER NoInstall
    Skip dependency installation/updates

.EXAMPLE
    PS> .\start-server.ps1
    # Starts server with all setup steps

.EXAMPLE
    PS> .\start-server.ps1 -NoMigrations -NoInstall
    # Starts server assuming environment is already prepared
#>

param(
    [string]$ProjectRoot = "",
    [switch]$NoMigrations,
    [switch]$NoInstall
)

# ============================================================================
# Configuration & Constants
# ============================================================================
$ErrorActionPreference = "Continue"  # Don't stop on first error
$WarningPreference = "Continue"

# Script location and project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $ScriptDir
}

$VenvDir = Join-Path $ProjectRoot "venv"
$VenvScripts = Join-Path $VenvDir "Scripts"
$PythonExe = Join-Path $VenvScripts "python.exe"
$PipExe = Join-Path $VenvScripts "pip.exe"
$RunScript = Join-Path $ProjectRoot "run.py"
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$EnvFile = Join-Path $ProjectRoot ".env"
$MigrateScript = Join-Path $ScriptDir "db_migrate.py"

$MIN_PYTHON_VERSION = "3.10"

# ============================================================================
# Helper Functions
# ============================================================================

function Write-Status {
    param([string]$Message, [string]$Type = "Info")
    $timestamp = Get-Date -Format "HH:mm:ss"
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
    Write-Host "$prefix [$timestamp] $Message" -ForegroundColor $color
}

function Test-ProjectStructure {
    Write-Status "Validating project structure..." "Info"

    $checks = @(
        @{ Path = $ProjectRoot; Name = "Project root" }
        @{ Path = $RunScript; Name = "run.py" }
        @{ Path = $EnvFile; Name = ".env configuration" }
        @{ Path = $RequirementsFile; Name = "requirements.txt" }
    )

    $allValid = $true
    foreach ($check in $checks) {
        if (Test-Path $check.Path) {
            Write-Status "  ✓ $($check.Name)" "Success"
        }
        else {
            Write-Status "  ✗ Missing: $($check.Name)" "Error"
            $allValid = $false
        }
    }

    return $allValid
}

function Find-Python {
    Write-Status "Checking Python installation..." "Info"

    try {
        $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCmd) {
            $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        }

        if ($pythonCmd) {
            $pythonPath = $pythonCmd.Source
            $version = & $pythonPath --version 2>&1
            Write-Status "  Found: $version at $pythonPath" "Success"

            # Check version
            if ($version -match 'Python\s+(\d+\.\d+)') {
                $pyVersion = [version]$matches[1]
                $minVersion = [version]$MIN_PYTHON_VERSION
                if ($pyVersion -ge $minVersion) {
                    return $pythonPath
                }
                else {
                    Write-Status "  Python version $pyVersion is below required $MIN_PYTHON_VERSION" "Error"
                    return $null
                }
            }
        }
    }
    catch { }

    Write-Status "  Python not found or version check failed" "Error"
    Write-Status "  Please install Python 3.10+ from https://www.python.org" "Error"
    return $null
}

function Initialize-Venv {
    param([string]$Python)

    if (Test-Path $VenvDir) {
        Write-Status "Virtual environment already exists" "Info"
        return $true
    }

    Write-Status "Creating virtual environment..." "Info"
    try {
        & $Python -m venv $VenvDir
        if ($LASTEXITCODE -eq 0) {
            Write-Status "  ✓ Virtual environment created" "Success"
            return $true
        }
        else {
            Write-Status "  ✗ Failed to create venv (exit code: $LASTEXITCODE)" "Error"
            return $false
        }
    }
    catch {
        Write-Status "  ✗ Error creating venv: $_" "Error"
        return $false
    }
}

function Install-Dependencies {
    Write-Status "Installing Python dependencies..." "Info"

    if (-not (Test-Path $RequirementsFile)) {
        Write-Status "  ✗ requirements.txt not found" "Error"
        return $false
    }

    try {
        Write-Status "  Running: pip install -r requirements.txt" "Info"
        $output = & $PipExe install -q -r $RequirementsFile 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Status "  ✓ Dependencies installed" "Success"
            return $true
        }
        else {
            Write-Status "  ✗ pip install failed (exit code: $LASTEXITCODE)" "Error"
            Write-Verbose "pip output: $output"
            return $false
        }
    }
    catch {
        Write-Status "  ✗ Error installing dependencies: $_" "Error"
        return $false
    }
}

function Run-Migrations {
    Write-Status "Running database migrations..." "Info"

    if (-not (Test-Path $MigrateScript)) {
        Write-Status "  ! Migration script not found, skipping" "Warning"
        return $true
    }

    try {
        Write-Status "  Running: alembic upgrade head" "Info"
        # Change to project root for migrations
        Push-Location $ProjectRoot
        $output = & $PythonExe scripts/db_migrate.py upgrade 2>&1
        Pop-Location

        if ($LASTEXITCODE -eq 0) {
            Write-Status "  ✓ Migrations completed" "Success"
            return $true
        }
        else {
            Write-Status "  ✗ Migrations failed (exit code: $LASTEXITCODE)" "Warning"
            Write-Verbose "Migration output: $output"
            # Don't treat migration failure as hard error - server might still start
            return $true
        }
    }
    catch {
        Write-Status "  ✗ Error running migrations: $_" "Warning"
        return $true
    }
}

function Start-Server {
    Write-Status "Starting CommClient-Server..." "Info"
    Write-Status "Press Ctrl+C to stop the server" "Info"
    Write-Host ""

    try {
        Push-Location $ProjectRoot

        # Display startup info
        Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
        Write-Host "  CommClient-Server is starting..." -ForegroundColor Green
        Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
        Write-Host ""

        # Run server
        & $PythonExe run.py

        Pop-Location
        return $LASTEXITCODE
    }
    catch {
        Write-Status "Error starting server: $_" "Error"
        Pop-Location
        return 1
    }
}

function Show-StartupInfo {
    Write-Host ""
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  Server Startup Complete" -ForegroundColor Green
    Write-Host "════════════════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host ""
    Write-Host "Server Address:    http://localhost:3000" -ForegroundColor Cyan
    Write-Host "API Docs:          http://localhost:3000/docs" -ForegroundColor Cyan
    Write-Host "WebSocket:         ws://localhost:3000/socket.io" -ForegroundColor Cyan
    Write-Host "Project Root:      $ProjectRoot" -ForegroundColor Cyan
    Write-Host "Python Venv:       $VenvDir" -ForegroundColor Cyan
    Write-Host "Database:          ./data/commclient.db" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Logs: ./data/logs/" -ForegroundColor Yellow
    Write-Host ""
}

function Setup-ExitHandler {
    $null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
        Write-Host ""
        Write-Status "Shutting down server..." "Warning"
        # Process will be terminated by the trap below
    }

    $null = $host.UI.RawUI.CancelKeyPress
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Host ""
    Write-Host "╔════════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║  CommClient-Server Development Launcher                        ║" -ForegroundColor Cyan
    Write-Host "╚════════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""

    # Validate project structure
    if (-not (Test-ProjectStructure)) {
        Write-Status "Project structure validation failed!" "Error"
        exit 1
    }

    # Find Python
    $pythonPath = Find-Python
    if (-not $pythonPath) {
        exit 1
    }

    # Initialize virtual environment
    if (-not (Initialize-Venv $pythonPath)) {
        exit 1
    }

    # Install dependencies
    if (-not $NoInstall) {
        if (-not (Install-Dependencies)) {
            Write-Status "Dependency installation failed!" "Error"
            exit 1
        }
    }
    else {
        Write-Status "Skipping dependency installation" "Warning"
    }

    # Run migrations
    if (-not $NoMigrations) {
        $null = Run-Migrations
    }
    else {
        Write-Status "Skipping database migrations" "Warning"
    }

    # Show startup info
    Show-StartupInfo

    # Start the server
    $exitCode = Start-Server

    Write-Host ""
    Write-Status "Server stopped with exit code: $exitCode" "Info"
    exit $exitCode
}

# Trap Ctrl+C for graceful shutdown
trap {
    Write-Host ""
    Write-Status "Server interrupted by user" "Warning"
    exit 0
}

Main
