# CommClient-Server Deployment Scripts

Production-ready Windows deployment and management scripts for CommClient-Server.

## Overview

This directory contains PowerShell and batch scripts for:
- Service installation and management
- Development server startup
- Health monitoring and auto-recovery
- Database backup and rotation
- Firewall configuration

All scripts are designed for IT deployment in office environments with comprehensive error handling, logging, and user feedback.

## Quick Reference

| Script | Purpose | Mode | Admin Required |
|--------|---------|------|-----------------|
| `install-service.ps1` | Install Windows service | Admin | Yes |
| `uninstall-service.ps1` | Remove Windows service | Admin | Yes |
| `start-server.ps1` | Development server | PowerShell | No |
| `start-server.bat` | Development server | Batch | No |
| `health-check.ps1` | Monitor & auto-restart | PowerShell | No* |
| `backup-db.ps1` | Database backup | PowerShell | No** |
| `setup-firewall.ps1` | Configure firewall | Admin | Yes |

*Restart requires admin  
**Restart requires admin

## Installation

### 1. Service Installation (Recommended)

```powershell
# Run as Administrator
cd C:\path\to\CommClient-Server
.\scripts\install-service.ps1
```

This creates:
- Windows service "CommClientServer"
- Auto-starts on boot
- Restarts on failure
- Logs to `data/logs/service.log`

### 2. Development Mode

```powershell
# No admin required
.\scripts\start-server.ps1
```

---

## Script Descriptions

### install-service.ps1
Installs CommClient-Server as a Windows service using NSSM.

**Automatic:**
- Python 3.10+ validation
- Virtual environment setup
- Dependency installation
- NSSM download/installation (if needed)
- Environment variable loading from .env
- Service startup configuration

**Manual Setup Steps:**
```powershell
.\scripts\install-service.ps1 -ServiceName "MyService" -ProjectRoot "C:\CommClient"
```

### uninstall-service.ps1
Safely removes the Windows service.

```powershell
.\scripts\uninstall-service.ps1 -ServiceName "CommClientServer"
```

### start-server.ps1
Starts server in development mode with full setup.

```powershell
# Standard startup
.\scripts\start-server.ps1

# Skip migrations (if already current)
.\scripts\start-server.ps1 -NoMigrations

# Skip dependency installation
.\scripts\start-server.ps1 -NoInstall
```

Displays:
- Server URL: http://localhost:3000
- API docs: http://localhost:3000/docs
- WebSocket: ws://localhost:3000/socket.io

### start-server.bat
Batch version for cmd.exe users.

```batch
scripts\start-server.bat --no-migrations
```

### health-check.ps1
Monitors server health and optionally auto-restarts.

```powershell
# Check health
.\scripts\health-check.ps1

# Check and auto-restart if down
.\scripts\health-check.ps1 -AutoRestart

# Custom endpoint
.\scripts\health-check.ps1 -Endpoint "http://192.168.1.100:3000/api/health"
```

**Scheduled Task (every 5 minutes):**
```powershell
powershell.exe -ExecutionPolicy Bypass -File ".\scripts\health-check.ps1" -AutoRestart
```

Logs to: `data/logs/health.log`

### backup-db.ps1
Creates database backups with automatic rotation.

```powershell
# Simple backup
.\scripts\backup-db.ps1

# With compression
.\scripts\backup-db.ps1 -Compress

# Keep last 20 backups
.\scripts\backup-db.ps1 -MaxBackups 20

# API backup with token
.\scripts\backup-db.ps1 -ApiToken "eyJ..." -Compress
```

Backups stored in: `data/backups/`

**Scheduled Daily Backup (2 AM):**
```powershell
$trigger = New-ScheduledTaskTrigger -Daily -At "02:00"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-ExecutionPolicy Bypass -File ".\scripts\backup-db.ps1" -Compress'
Register-ScheduledTask -TaskName "CommClientBackup" -Trigger $trigger -Action $action
```

### setup-firewall.ps1
Configures Windows Firewall for server operation.

```powershell
# Enable all rules for all profiles
.\scripts\setup-firewall.ps1

# Enable for private/domain only
.\scripts\setup-firewall.ps1 -ProfileType "Private, Domain"

# Disable all rules
.\scripts\setup-firewall.ps1 -Disable

# Remove all rules
.\scripts\setup-firewall.ps1 -Remove

# List current rules
.\scripts\setup-firewall.ps1 -ListRules
```

Opens ports:
- **TCP 3000**: HTTP + Socket.IO
- **UDP 41234**: Discovery broadcast
- **UDP 40000-49999**: Mediasoup RTP
- **UDP 5353**: mDNS

---

## Common Tasks

### Start Service
```powershell
Start-Service CommClientServer
```

### Stop Service
```powershell
Stop-Service CommClientServer -Force
```

### Restart Service
```powershell
Restart-Service CommClientServer
```

### View Status
```powershell
Get-Service CommClientServer | Format-List
```

### View Logs
```powershell
# Last 50 lines
Get-Content .\data\logs\service.log -Tail 50

# Follow in real-time
Get-Content .\data\logs\service.log -Tail 100 -Wait
```

### Create Backup
```powershell
.\scripts\backup-db.ps1 -Compress
```

### Restore Backup
```powershell
Stop-Service CommClientServer
Copy-Item .\data\backups\commclient_YYYYMMDD_HHMMSS.db `
  -Destination .\data\commclient.db -Force
Start-Service CommClientServer
```

### Check Health
```powershell
.\scripts\health-check.ps1
```

### Configure Firewall
```powershell
.\scripts\setup-firewall.ps1
```

### Uninstall Service
```powershell
.\scripts\uninstall-service.ps1
```

---

## Typical Deployment Sequence

### Initial Setup
```powershell
# 1. Run as Administrator
Start-Process powershell -Verb RunAs

# 2. Navigate to project
cd C:\CommClient-Server

# 3. Install service
.\scripts\install-service.ps1

# 4. Configure firewall
.\scripts\setup-firewall.ps1

# 5. Start service
Start-Service CommClientServer

# 6. Verify
Get-Service CommClientServer
Get-Content .\data\logs\service.log
```

### Daily Monitoring
```powershell
# Check service
Get-Service CommClientServer

# Health check with auto-restart
.\scripts\health-check.ps1 -AutoRestart

# View logs
Get-Content .\data\logs\service.log -Tail 50
```

### Weekly Maintenance
```powershell
# Backup database
.\scripts\backup-db.ps1 -Compress

# Review logs for errors
Select-String "ERROR" .\data\logs\service.log

# Check backups
Get-ChildItem .\data\backups | Sort-Object LastWriteTime -Desc
```

---

## Requirements

### For Service Installation
- Windows 10/Server 2016+
- Administrator privileges
- Python 3.10+ in PATH
- PowerShell 5.0+ (or 7.0+)
- ~500 MB disk space (with venv and deps)

### For Development Mode
- Python 3.10+ in PATH
- ~500 MB disk space
- No admin required

### For Firewall Configuration
- Administrator privileges
- Windows Defender Firewall (or compatible)

---

## Logging

All scripts provide comprehensive logging:

| Log File | Purpose | Retention |
|----------|---------|-----------|
| `data/logs/service.log` | Service output | Service lifetime |
| `data/logs/health.log` | Health checks | Auto-rotated at 10MB |
| `data/logs/backup.log` | Backup operations | Unlimited |

View logs:
```powershell
# Service logs
Get-Content .\data\logs\service.log -Tail 100 -Wait

# Health check logs
Get-Content .\data\logs\health.log

# Backup logs
Get-Content .\data\logs\backup.log
```

---

## Environment Variables

Scripts read from `.env` file in project root:

```env
HOST=0.0.0.0
PORT=3000
DEBUG=false
LOG_LEVEL=INFO
DB_BACKEND=sqlite
SQLITE_PATH=./data/commclient.db
JWT_SECRET=your-secret-here
DISCOVERY_UDP_PORT=41234
MEDIASOUP_MIN_PORT=40000
MEDIASOUP_MAX_PORT=49999
```

These are automatically loaded by the service.

---

## Troubleshooting

### Service Won't Start
1. Check logs: `Get-Content .\data\logs\service.log`
2. Verify Python: `python --version`
3. Test manually: `.\scripts\start-server.ps1`
4. Check NSSM: `nssm status CommClientServer`

### Port Already in Use
```powershell
# Find process
Get-NetTCPConnection -LocalPort 3000

# Kill process
Stop-Process -Id <PID> -Force

# Or use different port in .env
```

### Database Locked
```powershell
# Stop service
Stop-Service CommClientServer -Force

# Wait and restart
Start-Sleep -Seconds 5
Start-Service CommClientServer
```

### Firewall Blocking Connections
```powershell
# Check rules
.\scripts\setup-firewall.ps1 -ListRules

# Re-enable rules
.\scripts\setup-firewall.ps1 -Enable
```

---

## Files Structure

```
scripts/
├── install-service.ps1      # Install Windows service
├── uninstall-service.ps1    # Remove Windows service
├── start-server.ps1         # Development mode (PowerShell)
├── start-server.bat         # Development mode (Batch)
├── health-check.ps1         # Monitor & auto-restart
├── backup-db.ps1            # Database backup
├── setup-firewall.ps1       # Firewall configuration
├── db_migrate.py            # Database migration helper
├── README.md                # This file
├── DEPLOYMENT_GUIDE.md      # Detailed deployment guide
```

---

## Getting Help

For detailed information, see:
- `DEPLOYMENT_GUIDE.md` - Complete deployment instructions
- Script help: `Get-Help .\script-name.ps1`
- Logs: `data/logs/` directory
- Project README: `../README.md`

---

## License

These scripts are part of the CommClient-Server project and follow the same license.
