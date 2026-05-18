# Helen Mobile — Android Client

Capacitor wrapper around the same React renderer that the desktop app
ships with. Connects to any **Helen-Server** running on Windows, Linux,
or in a container — the protocol is server-agnostic (REST + Socket.IO).

## What's in the box

| File | Purpose |
|---|---|
| `Helen-Mobile-1.0.0-debug.apk` | Ready-to-install debug APK (4.7 MB) |
| `android/` | Generated Android Studio project |
| `www/` | Bundled web assets (synced from desktop renderer) |
| `capacitor.config.ts` | Capacitor configuration |
| `scripts/sync-renderer.mjs` | Copies the desktop build into `www/` |
| `scripts/mobile-bridge.js` | Translates `window.electronAPI` → Capacitor plugins |

## Installing the APK

1. Enable **Install unknown apps** for your file manager / browser
   (Settings → Apps → Special access → Install unknown apps).
2. Copy `Helen-Mobile-1.0.0-debug.apk` to the device (USB / cloud /
   AirDrop equivalent).
3. Tap to install.
4. On first launch, grant: **Microphone**, **Camera**, **Notifications**.

## Connecting to a server

The app discovers a Helen-Server in three ways (in order):

1. **Last-used URL** — instantly reconnects to the previous server.
2. **LAN probe** — tries common gateway IPs (`192.168.1.1:3000`,
   `192.168.0.1:3000`, etc.) for 5 seconds.
3. **Manual entry** — type the server URL in the onboarding screen
   (e.g. `http://192.168.1.50:3000`).

Works against:
- **Windows**: `Helen-Server.exe` from `CommClient-Server/dist/`
- **Linux**: `helen-server` systemd unit from `Linux-Server/`
- **Docker**: `Linux-Server/Dockerfile`

## Permissions

| Permission | Why |
|---|---|
| INTERNET | All HTTP/WebSocket traffic |
| RECORD_AUDIO | Voice calls |
| CAMERA | Video calls |
| MODIFY_AUDIO_SETTINGS | Speaker / earpiece switching |
| BLUETOOTH_CONNECT | Bluetooth headset routing |
| FOREGROUND_SERVICE_* | Keep calls alive when backgrounded |
| POST_NOTIFICATIONS | Incoming-call alerts |
| READ_MEDIA_IMAGES/VIDEO/AUDIO | File-drop / media gallery |
| VIBRATE | Incoming-call ringer |
| WAKE_LOCK | Keep screen on during calls |
| ACCESS_WIFI_STATE + CHANGE_WIFI_MULTICAST_STATE | mDNS server discovery |

The app declares cleartext-HTTP allowed only for RFC 1918 LAN ranges
(`10/8`, `172.16/12`, `192.168/16`) and `*.local`. Public-internet
servers must use HTTPS.

## Rebuilding from source

Prerequisites:

- Node.js 18+
- JDK 17 (Eclipse Temurin recommended)
- Android SDK with platform-34 + build-tools 34.0.0
- The desktop renderer must be built first: `cd ../CommClient-Desktop && npm run build:renderer`

```bash
cd CommClient-Mobile
npm install
npm run build:web         # syncs renderer/ → www/
npm run cap:sync          # copies www/ → android/app/src/main/assets/public
npm run android:debug     # gradle assembleDebug
# APK: android/app/build/outputs/apk/debug/app-debug.apk
```

## Architecture notes

The renderer is the **same React + zustand + WebRTC code** that runs
inside Electron on desktop. It calls into `window.electronAPI.*`. We
ship `scripts/mobile-bridge.js` which installs a shim mapping those
calls to Capacitor plugins:

| `electronAPI.*` | Maps to |
|---|---|
| `config.get/set` | `@capacitor/preferences` |
| `discovery.scan` | Custom HTTP probe (WebView can't do raw UDP) |
| `notifications.show` | `@capacitor/local-notifications` |
| `system.openExternal` | `@capacitor/app` |
| `network.getStatus` | `@capacitor/network` |
| `system.getInfo` | `@capacitor/device` |
| `server.start/stop` | No-op (mobile doesn't host a server) |
| `menu.*` | No-op (no menu bar on Android) |

Anything desktop-only (embedded server spawn, native menus, etc.) is
no-op'd with a debug warning so the renderer keeps running.

## Known limitations

- **No native screen-share** — getDisplayMedia is unavailable on
  Android WebView. Screen share only works for receiving streams
  shared by other participants. Sending screen would require a
  native MediaProjection plugin (~1 day of work to add).
- **No mDNS** — WebView can't open multicast sockets. Discovery
  falls back to HTTP probing common LAN IPs.
- **No code-signing** — debug APK only. Release/Play Store needs a
  keystore and `npm run android:release` with signing config.

## Roadmap

| Item | Effort |
|---|---|
| MediaProjection plugin for screen-share sending | 1 day |
| FCM push notifications (production grade) | 1 day |
| Release-signed APK + Play Store config | 0.5 day |
| Background sync via WorkManager | 0.5 day |
| Native call notification UI (CallStyle) | 1 day |
