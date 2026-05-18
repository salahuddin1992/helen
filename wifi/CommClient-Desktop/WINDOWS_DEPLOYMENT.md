# CommClient — Windows Deployment & LAN Discovery Notes

## Overview

CommClient uses **zero-configuration LAN discovery** to automatically find the server on the local network. This eliminates manual IP entry for end-users. The system uses two complementary protocols:

1. **UDP Broadcast** (primary) — port `41234`
2. **mDNS** (secondary) — `_commclient._tcp.local.`

Both require specific Windows Firewall rules to function correctly.

---

## 1. Windows Firewall Configuration

### Automatic (NSIS Installer)

The Electron Builder NSIS installer should add firewall rules automatically. Add these to `electron-builder.yml` under the `nsis` section:

```yaml
nsis:
  installerScript: "installer.nsi"
  include: "firewall-rules.nsh"
```

**firewall-rules.nsh:**
```nsis
!macro customInstall
  ; Allow CommClient backend to receive incoming connections
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="CommClient Server" dir=in action=allow protocol=TCP localport=3000 program="$INSTDIR\resources\server\CommClient-Server.exe" enable=yes'

  ; Allow UDP broadcast discovery (listening)
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="CommClient Discovery (UDP In)" dir=in action=allow protocol=UDP localport=41234 program="$INSTDIR\CommClient.exe" enable=yes'

  ; Allow UDP broadcast discovery (sending)
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="CommClient Discovery (UDP Out)" dir=out action=allow protocol=UDP remoteport=41234 program="$INSTDIR\resources\server\CommClient-Server.exe" enable=yes'

  ; Allow mDNS (Bonjour)
  nsExec::ExecToLog 'netsh advfirewall firewall add rule name="CommClient mDNS" dir=in action=allow protocol=UDP localport=5353 program="$INSTDIR\resources\server\CommClient-Server.exe" enable=yes'
!macroend

!macro customUnInstall
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="CommClient Server"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="CommClient Discovery (UDP In)"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="CommClient Discovery (UDP Out)"'
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="CommClient mDNS"'
!macroend
```

### Manual (Admin PowerShell)

If the installer doesn't add rules, or for development:

```powershell
# TCP 3000 — API + WebSocket server
New-NetFirewallRule -DisplayName "CommClient Server" -Direction Inbound -Protocol TCP -LocalPort 3000 -Action Allow

# UDP 41234 — Discovery broadcast (inbound listener)
New-NetFirewallRule -DisplayName "CommClient Discovery In" -Direction Inbound -Protocol UDP -LocalPort 41234 -Action Allow

# UDP 41234 — Discovery broadcast (outbound sender)
New-NetFirewallRule -DisplayName "CommClient Discovery Out" -Direction Outbound -Protocol UDP -RemotePort 41234 -Action Allow

# UDP 5353 — mDNS (optional, secondary discovery)
New-NetFirewallRule -DisplayName "CommClient mDNS" -Direction Inbound -Protocol UDP -LocalPort 5353 -Action Allow
```

### Verification

```powershell
# Confirm rules are active
netsh advfirewall firewall show rule name="CommClient Server"
netsh advfirewall firewall show rule name="CommClient Discovery In"
```

---

## 2. Network Requirements

### Required
- All devices **must be on the same LAN/WiFi network** (same subnet)
- **UDP broadcast must not be blocked** by the router/AP
  - Most home/office routers allow broadcast by default
  - Enterprise networks with **AP isolation** or **client isolation** will block discovery

### Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Can't find server" | Firewall blocking UDP 41234 | Add firewall rule (see above) |
| "Can't find server" | Devices on different subnets | Connect to the same WiFi/VLAN |
| "Can't find server" | AP isolation enabled | Disable client isolation on router, or use manual IP |
| Server found but can't connect | Firewall blocking TCP 3000 | Add TCP 3000 inbound rule |
| Intermittent discovery | WiFi power management | Disable WiFi power saving (see below) |

### WiFi Power Management (Windows)

Windows aggressively sleeps WiFi adapters, which can cause discovery timeouts:

```powershell
# Disable WiFi power management (admin)
powercfg /setactiSchemeIndex SUB_WIRELESS WLAN_POWER_MODE 1
```

Or via Device Manager:
1. Open **Device Manager** → **Network Adapters**
2. Right-click WiFi adapter → **Properties**
3. **Power Management** tab → Uncheck **Allow the computer to turn off this device**
4. **Advanced** tab → Set **Power Saving Mode** to **Off** (or **Maximum Performance**)

---

## 3. mDNS / Bonjour

CommClient uses mDNS as a secondary discovery mechanism. On Windows, mDNS support varies:

### Windows 10 (1809+) / Windows 11
- Built-in mDNS responder is included. No additional software needed.
- The built-in implementation handles `_commclient._tcp.local.` service browsing.

### Older Windows
- Install **Apple Bonjour** (bundled with iTunes) or **Bonjour Print Services**
- Download: https://support.apple.com/kb/DL999

### If mDNS Is Blocked
- mDNS is a secondary fallback. UDP broadcast is the primary mechanism.
- If neither works, users can enter the server IP manually via the "Advanced" section on the login screen.

---

## 4. Discovery Protocol Details

### UDP Broadcast Packet (JSON, UTF-8)

```json
{
  "type": "commclient-server",
  "server_id": "a1b2c3d4e5f6...",
  "host": "192.168.1.100",
  "port": 3000,
  "version": "1.0.0",
  "name": "CommClient Server",
  "uptime": 3600,
  "users_online": 5,
  "protocol": "http",
  "ts": 1712678400
}
```

- **Broadcast address**: `255.255.255.255:41234` (global) + subnet-directed (e.g., `192.168.1.255:41234`)
- **Interval**: Every 3 seconds
- **Server ID**: SHA256-based, stable across restarts (persisted in `.server_id` file)

### Verification Handshake

After receiving a broadcast, the client verifies via HTTP:

```
GET http://{host}:{port}/api/discovery
```

Returns same JSON payload. Client confirms `server_id` matches the broadcast.

### Multi-Server Ranking

When multiple servers are found, they are ranked:
1. **Verified** servers first (HTTP handshake confirmed)
2. **Highest uptime** (most stable)
3. **Most users online** (most active)

### Network Change Recovery

The Electron main process monitors network interfaces every 3 seconds:
- **WiFi disconnect**: All servers marked unverified, renderer notified
- **WiFi reconnect**: Discovery restarts after 2-second stabilization delay
- **Subnet change**: All servers re-verified on new network

---

## 5. Server Identity

The server generates a stable ID on first run:

```
SHA256(hostname + MAC_address + creation_timestamp)
```

Stored in: `{DATA_DIR}/.server_id`

This ensures:
- Server is recognized across restarts
- IP changes don't create duplicate entries
- Multiple servers on the same LAN are distinguishable

---

## 6. Build & Package Commands

### Full Build (from project root)

```bash
# Backend (PyInstaller)
cd CommClient-Server
pyinstaller CommClient.spec

# Frontend (Vite + Electron Builder)
cd CommClient-Desktop
npm run build
npx electron-builder --win
```

### Output
- **Installer**: `CommClient-Desktop/dist/CommClient Setup X.Y.Z.exe` (NSIS)
- **Portable**: `CommClient-Desktop/dist/win-unpacked/`

### Required Resources in Package
```
resources/
  server/
    CommClient-Server.exe     # PyInstaller output
    _internal/                # PyInstaller dependencies
  installer/
    icon.ico                  # App icon (256x256)
```

---

## 7. Windows Defender Exclusions (Optional)

For performance, exclude CommClient from real-time scanning:

```powershell
# Add exclusions (admin)
Add-MpPreference -ExclusionPath "$env:APPDATA\CommClient"
Add-MpPreference -ExclusionProcess "CommClient.exe"
Add-MpPreference -ExclusionProcess "CommClient-Server.exe"
```

---

## 8. Troubleshooting

### Server Not Starting
- Check logs: `%APPDATA%\CommClient\logs\server-*.log`
- Verify port 3000 is not in use: `netstat -ano | findstr :3000`
- Check server exe exists: `%LOCALAPPDATA%\Programs\CommClient\resources\server\CommClient-Server.exe`

### Discovery Not Working
1. Confirm both devices on same WiFi
2. Check firewall rules (see Section 1)
3. Test UDP broadcast manually:
   ```powershell
   # From server machine, check if broadcast is sending
   netstat -ano | findstr :41234
   ```
4. Try manual connection via Advanced section on login screen

### WebRTC Calls Failing
- CommClient uses LAN-only WebRTC (no STUN/TURN)
- Both devices must be on the same subnet
- If behind a VPN, ensure split-tunneling allows local traffic

### Logs Location
- **Server logs**: `%APPDATA%\CommClient\logs\`
- **Electron logs**: DevTools console (Ctrl+Shift+I in dev mode)
- **Database**: `%APPDATA%\CommClient\data\commclient.db`
