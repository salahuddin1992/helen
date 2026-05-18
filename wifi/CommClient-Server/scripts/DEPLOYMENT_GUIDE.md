# CommClient-Server Windows Deployment Guide

Complete guide for deploying CommClient-Server as a Windows service with automated management scripts.

## Quick Start

### 1. First-Time Installation (Admin PowerShell)

```powershell
# Navigate to project
cd C:\path\to\CommClient-Server

# Install as Windows service
.\scripts\install-service.ps1

# Configure firewall
.\scripts\setup-firewall.ps1

# Start the service
Start-Service CommClientServer

# Verify it's running
Get-Service CommClientServer
```

### 2. Development Mode

For development and testing without service installation:

```powershell
# Start server with full setup (venv, deps, migrations)
.\scripts\start-server.ps1

# Or use batch version (cmd.exe)
.\scripts\start-server.bat
```

### 3. Service Management

```powershell
# Start service
Start-Service CommClientServer

# Stop service
Stop-Service CommClientServer -Force

# Restart service
Restart-Service CommClientServer

# Check status
Get-Service CommClientServer

# View service logs
Get-Content .\data\logs\service.log -Tail 100 -Wait
```

---

## Script Reference

### install-service.ps1

Installs CommClient-Server as a Windows service using NSSM (Non-Sucking Service Manager).

**Features:**
- Automatic NSSM download and installation (if needed)
- Python 3.10+ validation
- Virtual environment creation
- Dependency installation
- Environment variables from .env file
- Automatic startup on boot
- Restart on failure policy
- Service logging to `data/logs/service.log`

**Usage:**

```powershell
# Standard installation
.\scripts\install-service.ps1

# Custom service name
.\scripts\install-service.ps1 -ServiceName "MyCommClient"

# Specify NSSM location
.\scripts\install-service.ps1 -NSSMPath "C:\nssm\nssm.exe"

# Specify project root
.\scripts\install-service.ps1 -ProjectRoot "C:\CommClient-Server"
```

**Parameters:**
- `-ServiceName`: Windows service name (default: CommClientServer)
- `-NSSMPath`: Path to NSSM executable (auto-detected if not provided)
- `-ProjectRoot`: Project root directory (auto-detected if not provided)

**Requirements:**
- Administrator privileges
- Python 3.10+ in PATH
- NSSM (automatically downloaded if not found)

**What it does:**
1. ✓ Validates project structure (.env, run.py, requirements.txt)
2. ✓ Checks/creates Python virtual environment
3. ✓ Installs all dependencies from requirements.txt
4. ✓ Finds or installs NSSM
5. ✓ Creates Windows service with proper configuration
6. ✓ Loads environment variables from .env
7. ✓ Sets up auto-restart on failure
8. ✓ Configures logging

---

### uninstall-service.ps1

Safely removes the CommClient-Server Windows service.

**Features:**
- Graceful service shutdown
- Complete service removal
- NSSM cleanup
- Confirmation prompts

**Usage:**

```powershell
# Remove default service
.\scripts\uninstall-service.ps1

# Remove custom service
.\scripts\uninstall-service.ps1 -ServiceName "MyService"
```

**Parameters:**
- `-ServiceName`: Windows service name to remove (default: CommClientServer)
- `-NSSMPath`: Path to NSSM executable (auto-detected if not provided)

**What it does:**
1. ✓ Validates administrator privileges
2. ✓ Confirms removal with user
3. ✓ Stops running service gracefully
4. ✓ Removes service via NSSM
5. ✓ Verifies complete removal
6. ✓ Suggests cleanup operations

---

### start-server.ps1

Starts CommClient-Server in development mode with full environment setup.

**Features:**
- Python 3.10+ validation
- Automatic venv creation
- Dependency installation/updates
- Database migrations (alembic)
- Graceful Ctrl+C shutdown
- Comprehensive status reporting

**Usage:**

```powershell
# Standard startup
.\scripts\start-server.ps1

# Skip migrations (if already up-to-date)
.\scripts\start-server.ps1 -NoMigrations

# Skip dependency installation
.\scripts\start-server.ps1 -NoInstall

# Both
.\scripts\start-server.ps1 -NoMigrations -NoInstall
```

**Parameters:**
- `-NoMigrations`: Skip database migration step
- `-NoInstall`: Skip dependency installation
- `-ProjectRoot`: Project root directory (auto-detected if not provided)

**Startup Info Displayed:**
- Server address: `http://localhost:3000`
- API docs: `http://localhost:3000/docs`
- WebSocket: `ws://localhost:3000/socket.io`
- Python venv location
- Database location
- Log directory

**Press Ctrl+C to stop gracefully.**

---

### start-server.bat

Batch version of `start-server.ps1` for cmd.exe users.

**Identical to PowerShell version but works in cmd.exe/batch context.**

**Usage:**

```batch
REM Standard startup
scripts\start-server.bat

REM Skip migrations
scripts\start-server.bat --no-migrations

REM Skip dependencies
scripts\start-server.bat --no-install
```

**Parameters:**
- `--no-migrations`: Skip database migrations
- `--no-install`: Skip dependency installation

---

### health-check.ps1

Monitors server health and optionally auto-restarts if down.

**Features:**
- HTTP GET health endpoint checking (`/api/health`)
- Automatic service restart on failure
- Detailed diagnostic logging
- Log rotation (keeps logs under 10MB)
- Detailed status information

**Usage:**

```powershell
# Check health (no auto-restart)
.\scripts\health-check.ps1

# Check and auto-restart if down
.\scripts\health-check.ps1 -AutoRestart

# Custom endpoint
.\scripts\health-check.ps1 -Endpoint "http://192.168.1.100:3000/api/health"

# Custom log location
.\scripts\health-check.ps1 -LogPath "C:\logs\commclient-health.log"
```

**Parameters:**
- `-Endpoint`: Health check URL (default: http://localhost:3000/api/health)
- `-ServiceName`: Service name to restart (default: CommClientServer)
- `-TimeoutSeconds`: HTTP request timeout (default: 10)
- `-LogPath`: Log file location (default: ./data/logs/health.log)
- `-AutoRestart`: Enable automatic service restart on failure

**Logging:**
- Logs to: `data/logs/health.log`
- Auto-rotates at 10MB
- Keeps last 5 rotated logs
- Timestamped entries with severity levels

**Scheduled Task Setup:**

Use Windows Task Scheduler to run health checks every 5 minutes:

1. Open Task Scheduler (tasksched.msc)
2. Create new basic task: "CommClient Health Check"
3. Trigger: Recurring, daily, repeat every 5 minutes
4. Action: Start a program
   - Program: `powershell.exe`
   - Arguments: `-ExecutionPolicy Bypass -File "C:\path\to\scripts\health-check.ps1" -AutoRestart`
5. Advanced: Check "Run with highest privileges"

---

### backup-db.ps1

Creates automated database backups with rotation and compression.

**Features:**
- API-based backup (when server running + token)
- Direct file backup (when server stopped)
- ZIP compression support
- Automatic rotation (keeps N backups)
- Database lock detection
- Integrity verification
- Comprehensive logging

**Usage:**

```powershell
# Simple backup
.\scripts\backup-db.ps1

# With compression
.\scripts\backup-db.ps1 -Compress

# Using API (requires auth token)
.\scripts\backup-db.ps1 -ApiToken "eyJhbGciOiJIUzI1NiIs..." -Compress

# Keep last 20 backups
.\scripts\backup-db.ps1 -MaxBackups 20

# Custom backup directory
.\scripts\backup-db.ps1 -BackupDir "C:\Backups\CommClient"
```

**Parameters:**
- `-BackupDir`: Backup directory (default: ./data/backups)
- `-MaxBackups`: Number of backups to keep (default: 10)
- `-ApiToken`: JWT token for API backup (optional)
- `-ApiEndpoint`: Backup API URL (default: http://localhost:3000/api/admin/backups)
- `-Compress`: ZIP compress backups (requires 7-Zip or PowerShell 5.0+)
- `-Force`: Skip confirmation prompts

**Backup Location:**
```
./data/backups/
├── commclient_20240408_143022.db          # Uncompressed
├── commclient_20240408_140512.db.zip      # Compressed
└── commclient_20240408_135001.db.zip
```

**Backup Methods:**
1. **API Backup** (preferred when server running):
   - Uses POST /api/admin/backups endpoint
   - Requires valid JWT token
   - Server-side initiated backup
   
2. **Direct File Backup** (when server stopped):
   - Copies SQLite database file directly
   - Includes retry logic for locked files
   - No authentication required

**Compression:**
- Uses 7-Zip if available (faster, better compression)
- Falls back to Windows built-in compression (PowerShell 5.0+)
- Typically reduces size to 20-30% of original

**Scheduled Backup:**

Daily backup at 2 AM:

```powershell
# Create scheduled task
$trigger = New-ScheduledTaskTrigger -Daily -At "02:00"
$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument '-ExecutionPolicy Bypass -File "C:\path\to\scripts\backup-db.ps1" -Compress'
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "CommClientBackup" `
  -Trigger $trigger -Action $action -Principal $principal
```

---

### setup-firewall.ps1

Configures Windows Firewall rules for CommClient-Server operation.

**Features:**
- Opens TCP 3000 (HTTP + Socket.IO)
- Opens UDP 41234 (Discovery broadcast)
- Opens UDP 40000-49999 (Mediasoup RTP)
- Opens UDP 5353 (mDNS)
- Per-profile configuration (Public/Private/Domain)
- Easy enable/disable/remove operations
- Connectivity verification

**Usage:**

```powershell
# Enable rules for all profiles
.\scripts\setup-firewall.ps1

# Enable for specific profiles only
.\scripts\setup-firewall.ps1 -ProfileType "Private, Domain"

# Disable all rules
.\scripts\setup-firewall.ps1 -Disable

# Remove all rules
.\scripts\setup-firewall.ps1 -Remove

# List current rules
.\scripts\setup-firewall.ps1 -ListRules
```

**Parameters:**
- `-ProfileType`: Firewall profile(s): Public, Private, Domain, or All (default: All)
- `-Enable`: Enable CommClient rules (default action)
- `-Disable`: Disable CommClient rules
- `-Remove`: Remove CommClient rules completely
- `-ListRules`: Show all CommClient firewall rules

**Firewall Rules Created:**

| Rule | Protocol | Port(s) | Purpose |
|------|----------|---------|---------|
| CommClient-HTTP-SocketIO | TCP | 3000 | Main server endpoint |
| CommClient-Discovery | UDP | 41234 | Device discovery broadcast |
| CommClient-Mediasoup-RTP | UDP | 40000-49999 | Media streams (calls, screen share) |
| CommClient-mDNS | UDP | 5353 | Network discovery (Bonjour/Avahi) |

**Profile Types:**

- **Public**: Networks in public locations (coffee shops, airports)
- **Private**: Home/office networks (trusted)
- **Domain**: Domain-joined corporate networks

Typically, enable for at least **Private** and **Domain**.

**Verify Rules:**

```powershell
# List all CommClient rules
Get-NetFirewallRule -DisplayName "*CommClient*" | Format-Table Name, Enabled, Profile

# Test connectivity to port 3000
Test-NetConnection -ComputerName localhost -Port 3000
```

---

## Typical Deployment Workflow

### Setup (First Time)

```powershell
# 1. Run as Administrator
Start-Process powershell -Verb RunAs

# 2. Navigate to project
cd C:\path\to\CommClient-Server

# 3. Install service
.\scripts\install-service.ps1
# [Accepts defaults, creates service]

# 4. Configure firewall
.\scripts\setup-firewall.ps1
# [Enables all rules for all profiles]

# 5. Start service
Start-Service CommClientServer

# 6. Verify startup
Get-Service CommClientServer
# Output: Running, CommClientServer

# 7. Check logs
Get-Content .\data\logs\service.log
```

### Daily Operations

```powershell
# Check service status
Get-Service CommClientServer

# View recent logs
Get-Content .\data\logs\service.log -Tail 50

# Restart service
Restart-Service CommClientServer

# Run health check
.\scripts\health-check.ps1 -AutoRestart

# Create backup
.\scripts\backup-db.ps1 -Compress
```

### Maintenance

```powershell
# Upgrade dependencies (offline)
Stop-Service CommClientServer
.\scripts\start-server.ps1  # Let it upgrade deps, then Ctrl+C
Start-Service CommClientServer

# Run migrations manually
python -m scripts.db_migrate status
python -m scripts.db_migrate upgrade head

# View backup history
Get-ChildItem .\data\backups | Sort-Object LastWriteTime -Desc

# Restore from backup (copy from backups dir)
Stop-Service CommClientServer
Copy-Item .\data\backups\commclient_YYYYMMDD_HHMMSS.db -Destination .\data\commclient.db -Force
Start-Service CommClientServer
```

### Uninstall

```powershell
# Run as Administrator
.\scripts\uninstall-service.ps1

# Remove firewall rules (optional)
.\scripts\setup-firewall.ps1 -Remove

# Clean up data (optional)
Remove-Item .\venv -Recurse -Force
Remove-Item .\data\backups -Recurse -Force
```

---

## Troubleshooting

### Service won't start

1. **Check Python version:**
   ```powershell
   python --version  # Must be 3.10+
   ```

2. **View service logs:**
   ```powershell
   Get-Content .\data\logs\service.log -Tail 100
   ```

3. **Check service status:**
   ```powershell
   Get-Service CommClientServer | Format-List
   ```

4. **Try manual start for diagnostics:**
   ```powershell
   .\scripts\start-server.ps1
   ```

### NSSM installation fails

1. **Manual download:**
   - Visit https://nssm.cc/download
   - Extract to `C:\nssm` or add to PATH

2. **Use alternate NSSM location:**
   ```powershell
   .\scripts\install-service.ps1 -NSSMPath "C:\nssm\nssm.exe"
   ```

### Port 3000 already in use

1. **Find process using port:**
   ```powershell
   Get-NetTCPConnection -LocalPort 3000
   ```

2. **Kill process (if necessary):**
   ```powershell
   Stop-Process -Id <PID> -Force
   ```

3. **Use alternate port:**
   - Edit `.env`: `PORT=3001`
   - Restart service

### Database locked error

1. **Check who has database open:**
   ```powershell
   [System.Diagnostics.Process]::GetProcessesByName("python")
   ```

2. **Stop service and wait:**
   ```powershell
   Stop-Service CommClientServer -Force
   Start-Sleep -Seconds 5
   Start-Service CommClientServer
   ```

### Health check failing

1. **Test endpoint manually:**
   ```powershell
   Invoke-WebRequest http://localhost:3000/api/health
   ```

2. **Check firewall:**
   ```powershell
   .\scripts\setup-firewall.ps1 -ListRules
   ```

3. **Check service logs:**
   ```powershell
   Get-Content .\data\logs\service.log -Tail 50 -Wait
   ```

### Backup fails

1. **Check disk space:**
   ```powershell
   Get-Volume
   ```

2. **Check database access:**
   - Ensure service isn't locking database
   - Try stopping service first

3. **Use direct backup (not API):**
   ```powershell
   Stop-Service CommClientServer
   .\scripts\backup-db.ps1
   ```

---

## Environment Variables

Key variables from `.env` file:

```env
# Server
HOST=0.0.0.0
PORT=3000
DEBUG=false
LOG_LEVEL=INFO

# Database
DB_BACKEND=sqlite
SQLITE_PATH=./data/commclient.db

# Security
JWT_SECRET=change-in-production
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=60
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7

# Network
DISCOVERY_UDP_PORT=41234
MEDIASOUP_MIN_PORT=40000
MEDIASOUP_MAX_PORT=49999

# File Upload
UPLOAD_DIR=./data/files
MAX_UPLOAD_SIZE_MB=100
```

These are automatically loaded by the service installation script.

---

## Production Best Practices

1. **Security:**
   - Change `JWT_SECRET` in `.env`
   - Use strong passwords for admin accounts
   - Disable public profile firewall rules
   - Enable HTTPS/TLS (reverse proxy)

2. **Availability:**
   - Enable health checks with auto-restart
   - Set up scheduled backups
   - Monitor service status regularly
   - Have backup plan

3. **Performance:**
   - Monitor disk usage
   - Check database size
   - Review logs for errors
   - Tune file upload limits

4. **Maintenance:**
   - Review logs weekly
   - Rotate old backups
   - Keep dependencies updated
   - Document custom changes

---

## API Health Endpoint

The service exposes a health check endpoint:

```
GET http://localhost:3000/api/health

Response (200 OK):
{
  "status": "healthy",
  "timestamp": "2024-04-08T14:30:22.123456",
  "uptime_seconds": 3600,
  "database": "connected",
  "version": "1.0.0"
}
```

Used by `health-check.ps1` for monitoring.

---

## Support

For issues:
1. Check logs in `./data/logs/`
2. Review troubleshooting section
3. Test manually with `start-server.ps1`
4. Check GitHub issues or project documentation
