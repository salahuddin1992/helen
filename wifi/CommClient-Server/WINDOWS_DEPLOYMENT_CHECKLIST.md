# CommClient-Server Windows Deployment Checklist

**Project:** CommClient-Server (FastAPI + Socket.IO backend)  
**Target:** Windows 10/Server 2016+ with Python 3.10+  
**Estimated Time:** 10-15 minutes

---

## Pre-Deployment Requirements

- [ ] Windows 10 or Server 2016+ (or later)
- [ ] Administrator access on target machine
- [ ] Python 3.10+ installed and in PATH
  - Test: `python --version` in PowerShell
  - Download: https://www.python.org/downloads/ (add to PATH)
- [ ] PowerShell 5.0+ (or PowerShell 7.0+)
  - Test: `$PSVersionTable.PSVersion` in PowerShell
- [ ] ~500 MB free disk space minimum
- [ ] Network access to project files
- [ ] Copy project to target: `C:\CommClient-Server\` (or preferred path)

---

## Step 1: Validate Environment (5 min)

```powershell
# Run in PowerShell (any user)

# Check Python
python --version
# Should output: Python 3.10.x or higher

# Check PowerShell
$PSVersionTable.PSVersion
# Should be 5.0 or higher

# Check disk space
Get-Volume
# Verify ~500 MB free

# Navigate to project
cd C:\CommClient-Server

# Verify project structure
ls -Force
# Should have: .env, run.py, requirements.txt, app/, scripts/
```

If any checks fail, address before proceeding.

---

## Step 2: Install as Windows Service (5 min)

```powershell
# 1. Run PowerShell as Administrator
Start-Process powershell -Verb RunAs

# 2. Navigate to project
cd C:\CommClient-Server

# 3. Run installation script
.\scripts\install-service.ps1

# 4. Script will:
#    - Validate Python installation
#    - Create virtual environment
#    - Install dependencies
#    - Download NSSM if needed
#    - Register Windows service "CommClientServer"
#    - Load environment variables from .env
#    - Set startup type to "Automatic"
#    - Configure restart on failure

# 5. Verify installation
Get-Service CommClientServer | Format-List
# Expected: Status = Stopped (we'll start it next)
#           StartType = Automatic
```

**Troubleshooting if install fails:**

- [ ] Check Python version: `python --version` must be 3.10+
- [ ] Check dependencies: Look at `requirements.txt` line count
- [ ] Check NSSM: Did download dialog appear? Accept download.
- [ ] Check .env: Ensure file exists and is readable
- [ ] Review script output for specific errors

---

## Step 3: Configure Firewall (2 min)

```powershell
# Still as Administrator

# Configure firewall rules
.\scripts\setup-firewall.ps1

# Script will:
#   - Open TCP 3000 (HTTP + WebSocket)
#   - Open UDP 41234 (Discovery broadcast)
#   - Open UDP 40000-49999 (Media streams)
#   - Open UDP 5353 (mDNS)
#   - Create rules for all profiles (Public, Private, Domain)

# Verify rules were created
.\scripts\setup-firewall.ps1 -ListRules

# Should show 4 rules all "Enabled"
```

**Note:** If behind corporate firewall, coordinate with network team.

---

## Step 4: Start Service

```powershell
# Still as Administrator

# Start the service
Start-Service CommClientServer

# Give it 5 seconds to start
Start-Sleep -Seconds 5

# Verify running
Get-Service CommClientServer
# Expected: Status = Running

# Check logs
Get-Content .\data\logs\service.log -Tail 20

# Expected in logs:
#   "Uvicorn running on http://0.0.0.0:3000"
#   No ERROR messages
```

**Troubleshooting if won't start:**

- [ ] Check logs: `Get-Content .\data\logs\service.log`
- [ ] Check port 3000: `Test-NetConnection localhost -Port 3000`
- [ ] Try manual start: `.\scripts\start-server.ps1`
- [ ] Review .env for invalid values
- [ ] Check database file exists: `ls .\data\commclient.db`

---

## Step 5: Verify Connectivity

```powershell
# Test from any PowerShell (no admin needed)

# Test HTTP endpoint
Invoke-WebRequest http://localhost:3000/api/health

# Expected response: HTTP 200 with health JSON
# {
#   "status": "healthy",
#   "timestamp": "2024-04-08T14:30:22.123456",
#   "database": "connected"
# }

# Test from another machine on network
Invoke-WebRequest http://192.168.1.100:3000/api/health
# (Use actual server IP, may need to adjust firewall)

# Test WebSocket (from client)
# ws://localhost:3000/socket.io
# Should connect successfully
```

**Troubleshooting if connectivity fails:**

- [ ] Service running? `Get-Service CommClientServer`
- [ ] Port open? `Test-NetConnection localhost -Port 3000`
- [ ] Firewall rules? `.\scripts\setup-firewall.ps1 -ListRules`
- [ ] Check logs: `Get-Content .\data\logs\service.log -Tail 50`

---

## Step 6: Set Up Automated Monitoring (2 min)

### Option A: Health Check Task (Recommended)

```powershell
# As Administrator
# Create scheduled task for 5-minute health checks

$trigger = New-ScheduledTaskTrigger -Daily -At "00:00" -RepetitionInterval (New-TimeSpan -Minutes 5)
$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument '-ExecutionPolicy Bypass -File "C:\CommClient-Server\scripts\health-check.ps1" -AutoRestart'
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName "CommClientHealthCheck" `
  -Trigger $trigger -Action $action -Principal $principal `
  -Description "Monitor CommClient-Server and auto-restart if down"

# Verify task created
Get-ScheduledTask -TaskName "CommClientHealthCheck"
```

### Option B: Daily Backup Task

```powershell
# As Administrator
# Create scheduled task for daily 2 AM backup

$trigger = New-ScheduledTaskTrigger -Daily -At "02:00"
$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument '-ExecutionPolicy Bypass -File "C:\CommClient-Server\scripts\backup-db.ps1" -Compress'
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName "CommClientBackup" `
  -Trigger $trigger -Action $action -Principal $principal `
  -Description "Daily CommClient database backup"

# Verify task created
Get-ScheduledTask -TaskName "CommClientBackup"
```

---

## Step 7: Document Configuration

- [ ] Record service name: `CommClientServer`
- [ ] Record server URL: `http://localhost:3000` (or `http://<IP>:3000`)
- [ ] Record API docs: `http://localhost:3000/docs`
- [ ] Record WebSocket: `ws://localhost:3000/socket.io`
- [ ] Record data directory: `C:\CommClient-Server\data`
- [ ] Record backup location: `C:\CommClient-Server\data\backups`
- [ ] Record log location: `C:\CommClient-Server\data\logs`

---

## Operational Procedures

### Daily Monitoring

```powershell
# Check service status
Get-Service CommClientServer

# View recent logs (last 50 lines)
Get-Content C:\CommClient-Server\data\logs\service.log -Tail 50

# Run health check
C:\CommClient-Server\scripts\health-check.ps1
```

### Weekly Tasks

```powershell
# Check backup status
Get-ChildItem C:\CommClient-Server\data\backups | Sort-Object LastWriteTime -Desc

# Review error logs
Select-String "ERROR\|WARN" C:\CommClient-Server\data\logs\*.log

# Verify firewall rules still in place
Get-NetFirewallRule -DisplayName "*CommClient*"
```

### Monthly Maintenance

```powershell
# Check database size
(Get-Item C:\CommClient-Server\data\commclient.db).Length / 1MB

# Rotate old backups manually (if needed)
Get-ChildItem C:\CommClient-Server\data\backups | Sort-Object LastWriteTime | Select-Object -Skip 10

# Review .env for updates needed
notepad C:\CommClient-Server\.env
```

---

## Restart Procedures

### Restart Service (0 downtime approach)

```powershell
# Graceful restart
Restart-Service CommClientServer

# Wait for startup
Start-Sleep -Seconds 5

# Verify
Get-Service CommClientServer
```

### Restart with Cleanup

```powershell
# Forceful restart
Stop-Service CommClientServer -Force
Start-Sleep -Seconds 2
Start-Service CommClientServer
Start-Sleep -Seconds 5

# Verify
Get-Service CommClientServer
```

### Upgrade Python Dependencies

```powershell
# 1. Stop service
Stop-Service CommClientServer -Force

# 2. Start server in dev mode (auto-upgrades deps)
C:\CommClient-Server\scripts\start-server.ps1
# [Let it run for 10 seconds, then Ctrl+C]

# 3. Start service
Start-Service CommClientServer
```

---

## Backup & Recovery

### Create Manual Backup

```powershell
# Compress backup (recommended)
C:\CommClient-Server\scripts\backup-db.ps1 -Compress

# View backups
Get-ChildItem C:\CommClient-Server\data\backups
```

### Restore from Backup

```powershell
# 1. Stop service
Stop-Service CommClientServer -Force

# 2. Restore backup file
Copy-Item `
  "C:\CommClient-Server\data\backups\commclient_YYYYMMDD_HHMMSS.db" `
  "C:\CommClient-Server\data\commclient.db" -Force

# 3. Start service
Start-Service CommClientServer
```

---

## Uninstall Procedures

### Complete Uninstall

```powershell
# 1. Run as Administrator

# 2. Uninstall service
C:\CommClient-Server\scripts\uninstall-service.ps1

# 3. Remove firewall rules
C:\CommClient-Server\scripts\setup-firewall.ps1 -Remove

# 4. Remove scheduled tasks
Unregister-ScheduledTask -TaskName "CommClientHealthCheck" -Confirm:$false
Unregister-ScheduledTask -TaskName "CommClientBackup" -Confirm:$false

# 5. Optional: Remove data directory
Remove-Item C:\CommClient-Server -Recurse -Force

# 6. Verify
Get-Service CommClientServer -ErrorAction SilentlyContinue
# Should output: Cannot find service
```

---

## Troubleshooting Reference

| Issue | Command | Expected Result |
|-------|---------|-----------------|
| Service won't start | `Get-Content .\data\logs\service.log` | Review error message |
| Can't reach server | `Test-NetConnection localhost -Port 3000` | TcpTestSucceeded: True |
| Port in use | `Get-NetTCPConnection -LocalPort 3000` | Find PID of process |
| Database locked | `Stop-Service CommClientServer -Force` | Service stops |
| Check Python | `python --version` | Python 3.10 or higher |
| Health check fails | `Invoke-WebRequest http://localhost:3000/api/health` | StatusCode: 200 |
| Firewall rules | `Get-NetFirewallRule -DisplayName "*CommClient*"` | 4 rules enabled |

---

## Post-Deployment Testing

- [ ] Server starts automatically on reboot
  - Test: Restart machine, verify `Get-Service CommClientServer` shows Running
  
- [ ] Health check task runs
  - Test: Check `.\data\logs\health.log` has recent entries
  
- [ ] Backup task runs
  - Test: Check `.\data\backups\` has today's backup
  
- [ ] Firewall rules active
  - Test: Run `.\scripts\setup-firewall.ps1 -ListRules`
  
- [ ] Logs being written
  - Test: Check `.\data\logs\service.log` has recent entries
  
- [ ] API accessible from network
  - Test: `Invoke-WebRequest http://<server-ip>:3000/api/health`
  
- [ ] WebSocket connections work
  - Test: Client connects to `ws://<server-ip>:3000/socket.io`

---

## Support Contacts

- **Project Repository:** Check project documentation for support channels
- **Python Issues:** https://www.python.org/
- **Windows Issues:** Contact Windows support or IT team
- **NSSM Documentation:** https://nssm.cc/
- **Firewall Issues:** Contact network team

---

## Sign-Off

- **Deployed By:** _________________ **Date:** ________
- **Tested By:** _________________ **Date:** ________
- **Approved By:** _________________ **Date:** ________

**Notes:**
```
_________________________________________________________________

_________________________________________________________________

_________________________________________________________________
```

---

## Quick Reference Card

**Copy and paste these commands for common tasks:**

```powershell
# Check status
Get-Service CommClientServer

# Start service
Start-Service CommClientServer

# Stop service
Stop-Service CommClientServer -Force

# Restart service
Restart-Service CommClientServer

# View logs
Get-Content .\data\logs\service.log -Tail 50 -Wait

# Test health
Invoke-WebRequest http://localhost:3000/api/health

# Create backup
C:\CommClient-Server\scripts\backup-db.ps1 -Compress

# Uninstall service
C:\CommClient-Server\scripts\uninstall-service.ps1
```

---

**Deployment Date:** _______  
**Deployed Version:** _______  
**Server IP/Hostname:** _______  
**Notes:** _________________________________________________________________
