# CommClient SFU worker

Node.js + mediasoup-based SFU for group calls with ≥ 5 participants.
Speaks an HTTP control API consumed by the Python server
(`app/services/topology_manager.py::MediasoupBridge`) and by the desktop
client (`renderer/services/call/MediasoupSFUAdapter.ts`).

## Install & run (Windows)

```powershell
cd CommClient-Server\sfu-worker
npm install
# mediasoup builds a native worker on install — needs Python 3, Visual Studio
# Build Tools (C++), and ~2 GB free. First install takes a few minutes.

# Configure (env vars)
$env:MEDIASOUP_ANNOUNCED_IP = "192.168.1.10"   # your LAN IP
$env:MEDIASOUP_CONTROL_PORT = "4443"
$env:MEDIASOUP_CONTROL_TOKEN = "change-me"

npm start
```

Then point the Python server at it:

```powershell
$env:MEDIASOUP_CONTROL_URL = "http://127.0.0.1:4443"
$env:MEDIASOUP_CONTROL_TOKEN = "change-me"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Build alongside the main app (PyInstaller+Electron one-shot)

Add to `CommClient.spec` or `build.ps1`:

```powershell
cd CommClient-Server\sfu-worker
npm install --production
# package with pkg or node-runtime next to the server binary
cd ..\..
pyinstaller CommClient.spec
npm run build
npx electron-builder --win
```

## Configuration env vars

| Env var | Default | Purpose |
|---|---|---|
| `MEDIASOUP_CONTROL_HOST` | `127.0.0.1` | HTTP bind host |
| `MEDIASOUP_CONTROL_PORT` | `4443` | HTTP bind port |
| `MEDIASOUP_CONTROL_TOKEN` | (unset) | Bearer token; if unset, API is unauthenticated (loopback only) |
| `MEDIASOUP_ANNOUNCED_IP` | auto | LAN IP to put in ICE candidates |
| `MEDIASOUP_NUM_WORKERS` | min(cores, 4) | mediasoup worker subprocesses |
| `MEDIASOUP_RTC_MIN_PORT` / `MEDIASOUP_RTC_MAX_PORT` | 40000-49999 | UDP/TCP media port range |
| `MEDIASOUP_LOG_LEVEL` | `warn` | mediasoup internal log level |
| `MEDIASOUP_ROUTER_IDLE_SEC` | `600` | Auto-close routers with no activity |

## HTTP control API

All routes require `Authorization: Bearer <token>` when `MEDIASOUP_CONTROL_TOKEN` is set.

| Route | Purpose |
|---|---|
| `POST /routers` | create/return router for a call |
| `DELETE /routers/:callId` | close router + all children |
| `POST /routers/:callId/transports` | create WebRtcTransport (send|recv) |
| `POST /routers/:callId/transports/:id/connect` | DTLS handshake |
| `POST /routers/:callId/transports/:id/produce` | client starts producing |
| `POST /routers/:callId/consume` | server builds consumer for peer |
| `POST /routers/:callId/consumers/:id/resume` | resume paused consumer |
| `POST /routers/:callId/consumers/:id/pause` | pause consumer |
| `POST /routers/:callId/peers/:peerId/leave` | cleanup peer state |
| `GET /healthz` | liveness |
| `GET /stats` | registry snapshot |

## Architecture notes

- One `mediasoup.Worker` per CPU core (capped at 4). RTC ports partitioned per worker.
- Routers sharded by FNV-1a(call_id) so all transports of the same call live in one worker.
- Routers closed on `DELETE` or after `MEDIASOUP_ROUTER_IDLE_SEC` of no activity.
- Fatal worker crash leaves the pool; the supervisor should restart the process.
- LAN-only: `announcedIp` must be reachable from every client.
