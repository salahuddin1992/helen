# CommClient — Windows Packaging Architecture & Deployment Guide

## Build Pipeline Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Build Pipeline                            │
│                                                              │
│  ┌──────────────────┐   ┌──────────────────┐                │
│  │ CommClient-Server │   │ CommClient-Desktop│                │
│  │ (Python/FastAPI)  │   │ (Electron/React)  │                │
│  └────────┬─────────┘   └────────┬──────────┘                │
│           │                       │                           │
│    PyInstaller              Vite + TSC                        │
│    (onedir mode)        (main + preload +                    │
│           │                renderer)                          │
│           ▼                       │                           │
│  dist/CommClient-Server/          ▼                           │
│  ├── CommClient-Server.exe   dist-electron/                  │
│  ├── *.dll                   ├── main/index.js               │
│  └── _internal/              ├── preload/index.js            │
│                              └── renderer/index.html         │
│           │                       │                           │
│           └───────┬───────────────┘                           │
│                   ▼                                           │
│           Electron Builder                                   │
│           (NSIS installer)                                   │
│                   │                                           │
│                   ▼                                           │
│           release/                                           │
│           ├── CommClient Setup 1.0.0.exe                     │
│           ├── CommClient Setup 1.0.0.exe.blockmap            │
│           └── BUILD_MANIFEST.txt                             │
└─────────────────────────────────────────────────────────────┘
```

## Installed Application Structure

After NSIS installation to `C:\Users\<user>\AppData\Local\Programs\CommClient\`:

```
CommClient/
├── CommClient.exe                     ← Electron app (main entry)
├── resources/
│   ├── app.asar                       ← Bundled renderer + main + preload
│   ├── server/                        ← PyInstaller backend (extraResources)
│   │   ├── CommClient-Server.exe
│   │   ├── *.dll
│   │   └── _internal/
│   │       ├── migrations/
│   │       └── (Python stdlib + packages)
│   └── installer/
│       ├── icon.ico
│       └── uninstall.ico
├── *.dll                              ← Electron/Chromium native libs
├── locales/
└── LICENSE
```

## Runtime Data Paths (Production)

All persistent data under `%APPDATA%\CommClient\`:

```
%APPDATA%\CommClient\
├── data/
│   ├── commclient.db                 ← SQLite database
│   └── files/                        ← User-uploaded files
├── logs/
│   └── server-<timestamp>.log        ← Backend server logs
└── .credentials                      ← DPAPI-encrypted credential store
```

## Build Commands

### Prerequisites

```batch
:: Python 3.10+ with pip
python --version

:: Node.js 18+ with npm
node --version

:: Install dependencies (one-time)
scripts\setup.bat
```

### Development

```batch
:: Start both backend and frontend in dev mode
scripts\dev.bat

:: Backend only
scripts\dev.bat server

:: Frontend only (expects backend at localhost:3000)
scripts\dev.bat client
```

### Production Build

```batch
:: Full build: server → frontend → installer
scripts\build-release.bat

:: Skip server rebuild (reuse existing .exe)
scripts\build-release.bat --skip-server

:: Override version
scripts\build-release.bat --version 1.2.0

:: Unpacked directory build (faster, for testing)
scripts\build-release.bat --dir-only
```

### Release Staging

```batch
:: Stage release artifacts with checksums and install docs
scripts\release.bat

:: Also create a ZIP for distribution
scripts\release.bat --zip

:: Custom output directory
scripts\release.bat --output D:\releases
```

### Individual Build Steps (Manual)

```batch
:: 1. Build server (from CommClient-Server/)
cd CommClient-Server
call venv\Scripts\activate.bat
pyinstaller CommClient-Server.spec --noconfirm --clean

:: 2. Build frontend (from CommClient-Desktop/)
cd CommClient-Desktop
npm run build:renderer

:: 3. Package installer (from CommClient-Desktop/)
npx electron-builder --win --config electron-builder.yml

:: 4. Unpacked build (for quick testing)
npx electron-builder --win --dir --config electron-builder.yml
```

## Server Lifecycle (Production)

Electron main process manages the backend:

1. **Startup**: `app.whenReady()` → `startBackendServer()` → spawn `CommClient-Server.exe`
2. **Health check**: Polls `http://127.0.0.1:3000/api/health` every 500ms (30s timeout)
3. **Environment**: Passes absolute paths via env vars (`SQLITE_PATH`, `UPLOAD_DIR`, `LOG_DIR`)
4. **Logging**: Server stdout/stderr piped to `%APPDATA%\CommClient\logs\server-<ts>.log`
5. **Shutdown**: `before-quit` → `stopBackendServer()` → `taskkill /pid <PID> /t /f`
6. **Crash recovery**: Server `exit` event logged; user must restart CommClient

## Firewall Requirements

| Port | Protocol | Direction | Purpose |
|------|----------|-----------|---------|
| 3000 | TCP | Inbound | HTTP API + WebSocket (Socket.IO) |
| 41234 | UDP | Inbound + Outbound | LAN server discovery (broadcast) |
| 40000-49999 | TCP/UDP | Inbound + Outbound | WebRTC media (reserved for mediasoup) |

## LAN Deployment Strategy

1. **Install on all machines** — each has its own CommClient instance
2. **First launch** — the first machine to start acts as server; it broadcasts presence via mDNS + UDP on port 41234
3. **Auto-discovery** — other clients detect the server automatically; no manual IP configuration needed
4. **All traffic stays on LAN** — no internet, no cloud, no external STUN/TURN servers

## Code Signing (Optional)

To sign the installer with a code signing certificate:

```yaml
# In electron-builder.yml, uncomment:
win:
  certificateFile: path/to/cert.pfx
  certificatePassword: ""
  publisherName: "Your Company Name"
```

Or pass via environment variable:
```batch
set CSC_LINK=path\to\cert.pfx
set CSC_KEY_PASSWORD=your_password
scripts\build-release.bat
```

## Troubleshooting Build Issues

| Issue | Solution |
|-------|---------|
| PyInstaller fails: `ModuleNotFoundError` | Add missing module to `hidden_imports` in `.spec` file |
| Electron Builder fails: `extraResources not found` | Ensure `CommClient-Server\dist\CommClient-Server\` exists |
| NSIS error: `icon not found` | Verify `resources\installer\icon.ico` exists (256x256, 32-bit) |
| TypeScript errors | Run `npx tsc --noEmit` to see errors; fix or suppress |
| Server won't start in packaged app | Check `%APPDATA%\CommClient\logs\` for server error logs |
| `EPERM` on Windows | Run build from non-OneDrive directory; close file explorer in release\ |
