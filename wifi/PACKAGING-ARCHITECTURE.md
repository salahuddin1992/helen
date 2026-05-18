# CommClient — Windows Packaging Architecture

## Overview

CommClient ships as a **single NSIS installer** (`CommClient-Setup-x.y.z.exe`) that embeds both the Electron desktop app and the Python FastAPI backend server. No separate Python or Node.js installation is required on target machines.

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                  NSIS Installer (.exe)                   │
│                                                         │
│  ┌───────────────────────┐  ┌────────────────────────┐  │
│  │  Electron App (asar)  │  │  extraResources/       │  │
│  │  ┌─────────────────┐  │  │  └── server/           │  │
│  │  │ dist-electron/   │  │  │      ├── CommClient-   │  │
│  │  │  ├── main/       │  │  │      │   Server.exe    │  │
│  │  │  ├── preload/    │  │  │      ├── _internal/    │  │
│  │  │  └── renderer/   │  │  │      └── migrations/   │  │
│  │  └─────────────────┘  │  └────────────────────────┘  │
│  └───────────────────────┘                              │
└─────────────────────────────────────────────────────────┘

Runtime:
┌──────────────────────────────────────────────────────────┐
│  Electron Main Process                                   │
│  ├── Spawns CommClient-Server.exe (child process)        │
│  ├── Waits for /docs health check (30s timeout)          │
│  ├── Creates BrowserWindow → loads renderer              │
│  └── On quit: kills server process                       │
│                                                          │
│  ┌──────────────┐    HTTP/WS      ┌──────────────────┐  │
│  │   Renderer    │ ─────────────→  │  FastAPI Server   │  │
│  │   (React)     │  localhost:3000 │  (CommClient-     │  │
│  │              │ ←─────────────  │   Server.exe)     │  │
│  └──────────────┘   Socket.IO     └──────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

---

## Build Pipeline

### Stage 1: Backend (PyInstaller)

```
CommClient-Server/
  ├── run.py                  ← entry point
  ├── CommClient-Server.spec  ← PyInstaller spec
  └── app/                    ← FastAPI application
        ↓
  pyinstaller CommClient-Server.spec
        ↓
  dist/CommClient-Server/
    ├── CommClient-Server.exe  ← standalone executable (no Python needed)
    ├── _internal/             ← bundled Python runtime + packages
    └── migrations/            ← Alembic migrations (data)
```

**Key decisions:**
- `console=False` — no cmd window when launched by Electron
- Single-folder mode (not one-file) — faster startup, easier debugging
- UPX compression enabled — reduces binary size
- Dev/test packages excluded (pytest, httpx, etc.)

### Stage 2: Frontend (Vite + Electron)

```
CommClient-Desktop/
  ├── src/main/index.ts       ← Electron main process
  ├── src/preload/index.ts    ← context bridge
  └── src/renderer/           ← React app
        ↓
  npx vite build
        ↓
  dist-electron/
    ├── main/index.js          ← compiled main process
    ├── preload/index.js       ← compiled preload
    └── renderer/              ← bundled React SPA
        ├── index.html
        ├── assets/
        └── ...
```

### Stage 3: Installer (electron-builder + NSIS)

```
  electron-builder --win --config
        ↓
  release/
    ├── CommClient-Setup-1.0.0.exe   ← NSIS installer (~80-120 MB)
    ├── latest.yml                   ← auto-update manifest
    └── builder-effective-config.yml
```

**electron-builder config highlights:**
- `extraResources`: embeds `CommClient-Server/dist/CommClient-Server/` → `resources/server/`
- `asar: true` — packs renderer/main into app.asar for performance
- NSIS: per-user install (no admin), custom install dir, desktop+start menu shortcuts

---

## Runtime File Locations

### Installed Files

```
C:\Users\<user>\AppData\Local\Programs\CommClient\   (default)
  ├── CommClient.exe                ← Electron launcher
  ├── resources/
  │   ├── app.asar                  ← packed Electron app
  │   ├── server/                   ← PyInstaller output
  │   │   ├── CommClient-Server.exe
  │   │   ├── _internal/
  │   │   └── migrations/
  │   └── installer/
  │       ├── icon.ico
  │       └── icon.png
  └── [electron runtime files]
```

### User Data (Portable)

```
%APPDATA%\CommClient\              ← app.getPath('appData') + 'CommClient'
  ├── data/
  │   ├── commclient.db            ← SQLite database
  │   └── files/                   ← uploaded files
  └── logs/
      └── server-<timestamp>.log   ← backend server logs
```

**Important:** Data directory is NOT removed on uninstall (`deleteAppDataOnUninstall: false`), preserving user data across reinstalls.

---

## Server Lifecycle

1. **Startup:** Electron main process spawns `CommClient-Server.exe` with environment variables:
   - `HOST=0.0.0.0`
   - `PORT=3000`
   - `SQLITE_PATH=<absolute path to commclient.db>`
   - `UPLOAD_DIR=<absolute path to files/>`

2. **Health check:** Main process polls `http://127.0.0.1:3000/docs` every 500ms until it responds (max 30s timeout).

3. **Monitoring:** Server stdout/stderr piped to `logs/server-<timestamp>.log`.

4. **Shutdown:** On `before-quit`, Electron sends `taskkill /pid <PID> /t /f` to cleanly terminate the server and its child processes.

5. **Crash recovery:** If server exits unexpectedly, Electron shows an error dialog with the log file path.

---

## Build Commands

### Full build (recommended)

```batch
REM Windows CMD
build.bat

REM PowerShell
.\build.ps1
```

### Individual stages

```batch
REM Server only
build.bat server

REM Desktop frontend only
build.bat desktop

REM Package installer only (requires prior builds)
build.bat installer
```

### Release with versioning

```powershell
# Bump patch (1.0.0 → 1.0.1) and build
.\release.ps1

# Bump minor (1.0.1 → 1.1.0)
.\release.ps1 -BumpType minor

# Explicit version
.\release.ps1 -Version "2.0.0"

# Skip build (use existing artifacts)
.\release.ps1 -SkipBuild
```

Release output:

```
releases/v1.0.1/
  ├── CommClient-Setup-1.0.1.exe
  ├── SHA256SUMS.txt
  ├── release.json          ← machine-readable manifest
  └── CHANGELOG.md          ← edit before distributing
```

---

## Prerequisites (Build Machine Only)

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.10+ | Backend runtime + PyInstaller |
| Node.js | 18+ | Frontend build + Electron |
| npm | 9+ | Package management |
| PyInstaller | 6+ | Python → exe bundling |
| Windows SDK | 10+ | NSIS installer compilation |

**Target machines require nothing** — the installer is self-contained.

---

## Deployment Notes

### LAN Distribution
- Copy installer to a shared network drive
- Users run the .exe — no admin required (per-user install)
- Each client auto-discovers the server via LAN broadcast (port 41234 UDP)

### Multi-server Setup
- Run CommClient-Server.exe independently on a dedicated machine
- Configure clients to point to the server's IP via `.env` or UI settings
- Multiple clients can connect to the same server

### Firewall Rules
The installer does NOT configure firewall rules. For the server machine, open:
- **TCP 3000** — HTTP/WebSocket (Socket.IO)
- **UDP 41234** — LAN discovery broadcast
- **TCP 40000-49999** — mediasoup media ports (reserved)

### Windows Defender / Antivirus
PyInstaller executables may trigger false positives. Options:
- Code-sign the server .exe (recommended for production)
- Add exclusion for `%APPDATA%\CommClient\` and install directory

### Troubleshooting
- **Server won't start:** Check `%APPDATA%\CommClient\logs\server-*.log`
- **UI won't load:** Open DevTools (Ctrl+Shift+I in dev) → Console
- **Call debug:** Browser console → `__commclient_call_debug.enable()` then `copy(__commclient_call_debug.exportJSON())`
- **Data reset:** Delete `%APPDATA%\CommClient\data\` (preserves logs)
- **Clean reinstall:** Uninstall + delete `%APPDATA%\CommClient\`

---

## Security Notes

- Server binds to `0.0.0.0` — accessible from LAN only (no internet exposure assumed)
- JWT secrets are randomly generated per-install (`secrets.token_hex(32)`)
- CORS set to `*` — appropriate for LAN-only deployment
- Socket auth validates JWT on every connection
- File uploads restricted by extension whitelist and 100MB size limit
- No telemetry, no external network calls
