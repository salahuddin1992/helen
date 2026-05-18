<#
.SYNOPSIS
    Backup database for CommClient-Server

.DESCRIPTION
    Creates automated backups of the CommClient-Server database:
    - Via API if server is running (requires auth token)
    - Direct file copy if server is stopped
    - Automatic rotation (keeps last N backups)
    - Compression support for storage efficiency
    - Logging of all operations

.PARAMETER BackupDir
    Backup directory (default: ./data/backups)

.PARAMETER MaxBackups
    Number of old backups to keep (default: 10)

.PARAMETER ApiToken
    JWT token for API authentication (required if server is running)

.PARAMETER ApiEndpoint
    API endpoint for backup request (default: http://localhost:3000/api/admin/backups)

.PARAMETER Compress
    Compress backups with ZIP (requires 7-Zip or built-in Windows compression)

.PARAMETER Force
    Skip confirmation prompts

.EXAMPLE
    PS> .\backup-db.ps1
    # Backs up database and keeps last 10 backups

.EXAMPLE
    PS> .\backup-db.ps1 -ApiToken "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." -Compress
    # Uses API with authentication and compresses backup

.NOTES
    Backup locations:
    - Default SQLite: ./data/commclient.db
    - Backups: ./data/backups/commclient_YYYYMMDD_HHMMSS.db[.zip]
    - Logs: ./data/logs/backup.log
#>

param(
    [string]$BackupDir = "",
    [int]$MaxBackups = 10,
    [string]$ApiToken = "",
    [string]$ApiEndpoint = "http://localhost:3000/api/admin/backups",
    [switch]$Compress,
    [switch]$Force
)

# ============================================================================
# Configuration
# ============================================================================
$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

if (-not $BackupDir) {
    $BackupDir = Join-Path $ProjectRoot "data\backups"
}

$LogDir = Join-Path $ProjectRoot "data\logs"
$LogFile = Join-Path $LogDir "backup.log"
$DbPath = Join-Path $ProjectRoot "data\commclient.db"

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupFile = Join-Path $BackupDir "commclient_$timestamp.db"

# ============================================================================
# Helper Functions
# ============================================================================

function Write-Log {
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )

    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    }

    $logTimestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$logTimestamp] [$Level] $Message"

    $color = switch ($Level) {
        "ERROR" { "Red" }
        "WARN" { "Yellow" }
        "SUCCESS" { "Green" }
        default { "Cyan" }
    }
    Write-Host $logMessage -ForegroundColor $color

    Add-Content -Path $LogFile -Value $logMessage -ErrorAction SilentlyContinue
}

function Test-ProjectStructure {
    if (-not (Test-Path $DbPath)) {
        Write-Log "Database file not found: $DbPath" "ERROR"
        return $false
    }
    return $true
}

function Ensure-BackupDirectory {
    if (-not (Test-Path $BackupDir)) {
        Write-Log "Creating backup directory: $BackupDir" "INFO"
        New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    }
}

function Test-ServerRunning {
    try {
        $response = Invoke-WebRequest `
            -Uri "http://localhost:3000/api/health" `
            -Method Get `
            -TimeoutSec 5 `
            -ErrorAction SilentlyContinue

        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Backup-ViaApi {
    param([string]$Token, [string]$Endpoint)

    Write-Log "Attempting backup via API endpoint..." "INFO"

    if (-not $Token) {
        Write-Log "API token required for backup. Run: Get-CommClientToken" "WARN"
        return $null
    }

    try {
        $headers = @{
            "Authorization" = "Bearer $Token"
            "Content-Type" = "application/json"
        }

        $response = Invoke-WebRequest `
            -Uri $Endpoint `
            -Method Post `
            -Headers $headers `
            -TimeoutSec 60 `
            -ErrorAction Stop

        if ($response.StatusCode -eq 200) {
            $responseData = $response.Content | ConvertFrom-Json
            Write-Log "API backup initiated: $($responseData | ConvertTo-Json -Compress)" "INFO"
            return $BackupFile
        }
        else {
            Write-Log "API backup failed with status $($response.StatusCode)" "ERROR"
            return $null
        }
    }
    catch {
        Write-Log "API backup error: $($_.Exception.Message)" "ERROR"
        return $null
    }
}

function Backup-DirectFile {
    Write-Log "Performing direct database file backup..." "INFO"

    if (-not (Test-Path $DbPath)) {
        Write-Log "Database file not found: $DbPath" "ERROR"
        return $null
    }

    try {
        # Check if database is locked
        $locked = $false
        try {
            [System.IO.File]::Open($DbPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::None).Dispose()
        }
        catch {
            $locked = $true
        }

        if ($locked) {
            Write-Log "Warning: Database file may be locked by another process" "WARN"
        }

        # Copy with retry
        $maxRetries = 3
        $retryCount = 0

        while ($retryCount -lt $maxRetries) {
            try {
                Copy-Item -Path $DbPath -Destination $BackupFile -Force -ErrorAction Stop
                $fileSize = (Get-Item $BackupFile).Length
                Write-Log "Database backed up successfully (size: $('{0:N2}' -f ($fileSize / 1MB)) MB)" "SUCCESS"
                return $BackupFile
            }
            catch {
                $retryCount++
                if ($retryCount -lt $maxRetries) {
                    Write-Log "Copy attempt $retryCount failed, retrying in 2 seconds..." "WARN"
                    Start-Sleep -Seconds 2
                }
            }
        }

        Write-Log "Failed to backup database after $maxRetries attempts" "ERROR"
        return $null
    }
    catch {
        Write-Log "Error during direct backup: $($_.Exception.Message)" "ERROR"
        return $null
    }
}

function Compress-Backup {
    param([string]$FilePath)

    if (-not (Test-Path $FilePath)) {
        Write-Log "Cannot compress: file not found $FilePath" "ERROR"
        return $null
    }

    Write-Log "Compressing backup file..." "INFO"

    try {
        $zipPath = "$FilePath.zip"

        # Try 7-Zip first (if available)
        $7zip = Get-Command 7z.exe -ErrorAction SilentlyContinue
        if ($7zip) {
            Write-Log "Using 7-Zip for compression..." "INFO"
            & $7zip a -tzip $zipPath $FilePath | Out-Null
            Remove-Item $FilePath -Force
            $zipSize = (Get-Item $zipPath).Length
            $origSize = (Get-Item $FilePath).Length
            Write-Log "Compression completed: $('{0:P0}' -f ($zipSize / $origSize)) of original size" "SUCCESS"
            return $zipPath
        }

        # Fall back to Windows compression (PowerShell 5.0+)
        if ($PSVersionTable.PSVersion.Major -ge 5) {
            Write-Log "Using Windows compression..." "INFO"
            Compress-Archive -Path $FilePath -DestinationPath $zipPath -Force
            Remove-Item $FilePath -Force
            $zipSize = (Get-Item $zipPath).Length
            Write-Log "Compression completed" "SUCCESS"
            return $zipPath
        }

        Write-Log "Compression not available (requires 7-Zip or PowerShell 5.0+)" "WARN"
        return $FilePath
    }
    catch {
        Write-Log "Compression error: $($_.Exception.Message)" "WARN"
        return $FilePath
    }
}

function Rotate-OldBackups {
    Write-Log "Rotating old backups (keeping last $MaxBackups)..." "INFO"

    try {
        if (-not (Test-Path $BackupDir)) {
            return
        }

        $backups = Get-ChildItem -Path $BackupDir -Filter "commclient_*.db*" | Sort-Object LastWriteTime -Descending
        $toDelete = $backups | Select-Object -Skip $MaxBackups

        if ($toDelete) {
            Write-Log "Removing $($toDelete.Count) old backup(s)..." "INFO"
            $toDelete | Remove-Item -Force
            foreach ($file in $toDelete) {
                Write-Log "  Deleted: $($file.Name) ($('{0:N2}' -f ($file.Length / 1MB)) MB)" "INFO"
            }
            Write-Log "Rotation completed" "SUCCESS"
        }
        else {
            Write-Log "No backups to rotate" "INFO"
        }
    }
    catch {
        Write-Log "Error rotating backups: $_" "ERROR"
    }
}

function Show-BackupInfo {
    param([string]$BackupFile)

    if (-not (Test-Path $BackupFile)) {
        return
    }

    $file = Get-Item $BackupFile
    Write-Log "Backup Info:" "INFO"
    Write-Log "  File:     $($file.Name)" "INFO"
    Write-Log "  Size:     $('{0:N2}' -f ($file.Length / 1MB)) MB" "INFO"
    Write-Log "  Created:  $($file.CreationTime)" "INFO"
    Write-Log "  Location: $($file.Directory.FullName)" "INFO"
}

function Verify-Backup {
    param([string]$BackupFile)

    if (-not (Test-Path $BackupFile)) {
        Write-Log "Backup file not found for verification" "ERROR"
        return $false
    }

    Write-Log "Verifying backup integrity..." "INFO"

    try {
        $file = Get-Item $BackupFile
        if ($file.Length -eq 0) {
            Write-Log "Backup file is empty!" "ERROR"
            return $false
        }

        # For ZIP files, test compression
        if ($BackupFile -like "*.zip") {
            Add-Type -AssemblyName System.IO.Compression
            $zip = [System.IO.Compression.ZipFile]::OpenRead($BackupFile)
            $zip.Dispose()
        }

        Write-Log "Backup verification passed" "SUCCESS"
        return $true
    }
    catch {
        Write-Log "Backup verification failed: $_" "ERROR"
        Remove-Item $BackupFile -Force -ErrorAction SilentlyContinue
        return $false
    }
}

# ============================================================================
# Main Execution
# ============================================================================

function Main {
    Write-Log "═════════════════════════════════════════════════════════" "INFO"
    Write-Log "CommClient-Server Database Backup" "INFO"
    Write-Log "═════════════════════════════════════════════════════════" "INFO"

    # Validate project
    if (-not (Test-ProjectStructure)) {
        Write-Log "Project validation failed" "ERROR"
        exit 1
    }

    # Create backup directory
    Ensure-BackupDirectory

    # Determine backup method
    $backupFile = $null
    $serverRunning = Test-ServerRunning

    if ($serverRunning -and $ApiToken) {
        Write-Log "Server is running, using API backup method" "INFO"
        $backupFile = Backup-ViaApi $ApiToken $ApiEndpoint
    }
    elseif ($serverRunning -and -not $ApiToken) {
        Write-Log "Server is running but no API token provided, using direct backup" "WARN"
        $backupFile = Backup-DirectFile
    }
    else {
        Write-Log "Server is not running, using direct file backup" "INFO"
        $backupFile = Backup-DirectFile
    }

    if (-not $backupFile) {
        Write-Log "Backup failed" "ERROR"
        exit 1
    }

    # Compress if requested
    if ($Compress) {
        $compressedFile = Compress-Backup $backupFile
        if ($compressedFile) {
            $backupFile = $compressedFile
        }
    }

    # Verify backup
    if (-not (Verify-Backup $backupFile)) {
        exit 1
    }

    # Show backup info
    Show-BackupInfo $backupFile

    # Rotate old backups
    Rotate-OldBackups

    Write-Log "═════════════════════════════════════════════════════════" "INFO"
    Write-Log "Backup completed successfully" "SUCCESS"
    exit 0
}

Main
