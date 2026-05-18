# CommClient — LAN-Only Communication Platform

## Architecture Blueprint v1.0

---

## 1. System Overview

CommClient is a full-featured desktop communication platform designed to operate **exclusively** within a local WiFi / LAN environment. Zero Internet dependency for core features. It supports private and group audio/video calls, text messaging, and screen sharing — all routed through the local network.

### Design Principles

- **LAN-first**: All discovery, signaling, and media transport happen on the local network
- **Zero-config**: Users on the same network discover each other automatically via mDNS/UDP broadcast
- **Hybrid media routing**: 1-to-1 calls use direct P2P; group calls use a local SFU (Selective Forwarding Unit)
- **Offline-capable**: No cloud backend. The signaling server and SFU run on a designated machine inside the LAN
- **Desktop-first**: Electron shell for Windows, optimized for keyboard/mouse workflows
- **Extensible**: Clean separation between signaling, media, and application layers

---

## 2. Architecture Decision: Tech Stack

### 2.1 Client (Desktop App)

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Shell | **Electron 30+** | Cross-platform desktop shell, native OS integration (tray, notifications, window management) |
| UI Framework | **React 18 + TypeScript** | Component-based, large ecosystem, type safety |
| State Management | **Zustand** | Lightweight, no boilerplate, works well with React |
| Styling | **Tailwind CSS + shadcn/ui** | Rapid prototyping, consistent design system |
| Real-time Transport | **WebRTC (via simple-peer)** | Native browser API for audio/video/data channels |
| Signaling | **Socket.IO Client** | Reliable WebSocket transport with auto-reconnect |
| Screen Capture | **Electron desktopCapturer API** | Native screen/window capture, zero external deps |
| Local Storage | **SQLite (via better-sqlite3)** | Embedded database for chat history, user preferences |
| Build/Package | **electron-builder** | Windows installer (NSIS), auto-update support |

### 2.2 Server (LAN Server)

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Runtime | **Node.js 20 LTS** | Same language as client, fast prototyping |
| HTTP Framework | **Fastify** | High performance, schema validation, plugin architecture |
| WebSocket | **Socket.IO Server** | Rooms, namespaces, reliable delivery |
| SFU (Group Media) | **mediasoup v3** | Production-grade SFU, runs as Node.js native module, supports simulcast |
| Discovery | **mDNS (bonjour-service)** + **UDP broadcast** | Zero-config LAN discovery, dual mechanism for reliability |
| Database | **SQLite (via better-sqlite3)** | Embedded, zero-config, file-based persistence |
| Auth | **JWT (jsonwebtoken)** + local user registry | Lightweight token auth, no external IdP required |
| File Transfer | **Fastify multipart + chunked streams** | Large file support via HTTP chunked transfer |

### 2.3 Why NOT These Alternatives

| Rejected | Reason |
|----------|--------|
| Tauri | Smaller ecosystem for WebRTC, less mature media handling |
| Janus SFU | C-based, harder to embed/deploy as single binary with Node |
| Matrix/Synapse | Over-engineered for pure LAN, requires federation config |
| WebTorrent | Not suited for real-time media streams |
| gRPC | Overkill for signaling; Socket.IO's room model maps directly to group calls |
| PeerJS | Depends on cloud PeerJS server by default, limited SFU path |

---

## 3. P2P vs SFU: When to Use Each

This is the most critical architectural decision for a LAN communication system.

### 3.1 Decision Matrix

| Scenario | Routing | Reason |
|----------|---------|--------|
| 1-to-1 audio call | **Direct P2P** | Lowest latency, zero server load, both peers on same LAN |
| 1-to-1 video call | **Direct P2P** | Same as above; LAN bandwidth is sufficient |
| 1-to-1 screen share | **Direct P2P** | High bandwidth but LAN handles it (100Mbps+ typical) |
| 1-to-1 text chat | **Direct P2P DataChannel** | Instant delivery, no server relay needed |
| Group audio (3-8) | **SFU (mediasoup)** | Each participant sends 1 stream, SFU forwards to N-1. Without SFU, each peer sends N-1 streams (mesh collapses at 4+ users) |
| Group video (3-8) | **SFU (mediasoup)** | Essential. Mesh video at 4+ peers = bandwidth explosion. SFU enables simulcast (send multiple quality layers, server picks best for each receiver) |
| Group video (9-50) | **SFU + simulcast + last-N** | SFU forwards only the top N active speakers' video; others get audio-only or thumbnails |
| Group screen share | **SFU** | Single high-bandwidth stream forwarded efficiently |
| Group text chat | **Server-relayed (Socket.IO room)** | Server ensures message ordering, persistence, offline delivery |
| File transfer (1-to-1) | **Direct P2P DataChannel** | Fastest path, no server storage needed |
| File transfer (group) | **Server-relayed HTTP** | Server acts as temporary storage + multicast distribution |

### 3.2 Architecture Diagram (Logical)

```
┌──────────────────────────────────────────────────────────┐
│                    LAN / WiFi Network                    │
│                                                          │
│  ┌─────────┐     P2P (1:1)      ┌─────────┐            │
│  │ Client A ├───────────────────►│ Client B │            │
│  └────┬─────┘                    └────┬─────┘            │
│       │                               │                  │
│       │ Signaling (Socket.IO)         │ Signaling        │
│       │                               │                  │
│  ┌────▼───────────────────────────────▼─────┐           │
│  │           LAN Server (Node.js)           │           │
│  │                                          │           │
│  │  ┌──────────┐  ┌───────────┐  ┌───────┐│           │
│  │  │ Signaling│  │ mediasoup │  │ REST  ││           │
│  │  │ (Sck.IO) │  │   SFU     │  │ API   ││           │
│  │  └──────────┘  └───────────┘  └───────┘│           │
│  │  ┌──────────┐  ┌───────────┐           │           │
│  │  │  mDNS    │  │  SQLite   │           │           │
│  │  │Discovery │  │    DB     │           │           │
│  │  └──────────┘  └───────────┘           │           │
│  └──────────────────────────────────────────┘           │
│                                                          │
│  ┌─────────┐    SFU (group)     ┌─────────┐            │
│  │ Client C ├───────────────────►│ Client D │            │
│  └─────────┘                    └─────────┘            │
└──────────────────────────────────────────────────────────┘
```

### 3.3 P2P Connection Flow (1-to-1)

```
Client A                    Signaling Server                    Client B
   │                              │                                │
   ├── call:initiate ────────────►│                                │
   │                              ├── call:incoming ──────────────►│
   │                              │◄── call:accept ────────────────┤
   │◄── call:accepted ───────────┤                                │
   │                              │                                │
   │   === ICE/SDP Exchange (via signaling) ===                   │
   ├── signal:offer ─────────────►│── signal:offer ───────────────►│
   │◄── signal:answer ───────────┤◄── signal:answer ──────────────┤
   │◄── signal:ice-candidate ────┤◄── signal:ice-candidate ───────┤
   │                              │                                │
   │   === Direct P2P Media Stream (bypasses server) ===          │
   │◄════════════════════════════════════════════════════════════►│
   │           Audio / Video / Screen / DataChannel               │
```

### 3.4 SFU Connection Flow (Group)

```
Client A          Signaling Server / mediasoup          Client B, C, D
   │                        │                                │
   ├── room:join ──────────►│                                │
   │                        ├── router:create ───────┐      │
   │                        │◄───────────────────────┘      │
   │◄── transport:params ──┤                                │
   │                        │                                │
   │── produce(audio) ─────►│   (A's audio → SFU)          │
   │── produce(video) ─────►│   (A's video → SFU)          │
   │                        │                                │
   │                        │    SFU forwards to B,C,D      │
   │                        ├── consume(A.audio) ───────────►│
   │                        ├── consume(A.video) ───────────►│
   │                        │                                │
   │                        │◄── produce(B.audio) ──────────┤
   │                        │◄── produce(B.video) ──────────┤
   │◄── consume(B.audio) ──┤                                │
   │◄── consume(B.video) ──┤                                │
```

---

## 4. Component Design

### 4.1 Server Components

#### 4.1.1 Discovery Service (`discovery-service`)
- **mDNS Advertisement**: Publishes `_commclient._tcp` service on port 3000
- **UDP Broadcast Beacon**: Sends periodic UDP broadcast on port 5353 as fallback
- **Service Resolution**: Clients query mDNS or listen for UDP beacons to find the server IP
- **Health Heartbeat**: Clients send heartbeat every 5s; server marks offline after 15s silence

#### 4.1.2 Auth Service (`auth-service`)
- **Local User Registry**: Users stored in SQLite with bcrypt-hashed passwords
- **JWT Tokens**: Short-lived access tokens (1h) + long-lived refresh tokens (7d)
- **Session Management**: Track active sessions per user, enforce single-device or multi-device policy
- **No external IdP**: All auth is local. Optional LDAP integration in production phase

#### 4.1.3 Signaling Service (`signaling-service`)
- **Socket.IO Namespaces**: `/chat`, `/call`, `/presence`
- **Room Management**: Create/join/leave rooms for group communication
- **SDP/ICE Relay**: Forward WebRTC signaling messages between peers for P2P setup
- **Call State Machine**: Manages call lifecycle (ringing → active → ended)
- **Presence Tracking**: Online/offline/busy/in-call status via heartbeat + socket disconnect detection

#### 4.1.4 Media Service (`media-service`) — mediasoup wrapper
- **Router Management**: One Router per group call room (codecs: VP8/VP9/H264, Opus)
- **Transport Management**: WebRtcTransport per participant (send + receive)
- **Producer/Consumer Lifecycle**: Track all media producers and consumers per room
- **Simulcast Support**: Video producers send 3 spatial layers; SFU selects per-consumer
- **Active Speaker Detection**: `audioLevelObserver` to detect dominant speaker
- **Bandwidth Estimation**: Adapt consumer layers based on available bandwidth
- **Screen Share Handling**: Separate producer for screen share track, prioritized forwarding

#### 4.1.5 Chat Service (`chat-service`)
- **Message Storage**: SQLite with FTS5 for full-text search
- **Message Types**: text, file-metadata, system-event, reply, reaction
- **Delivery Receipts**: sent → delivered → read status tracking
- **Offline Queue**: Store messages for offline users, deliver on reconnect
- **Group Channels**: Persistent channels with member lists and message history

#### 4.1.6 File Service (`file-service`)
- **Chunked Upload**: Multipart upload with resumable support
- **Storage**: Local filesystem under `./data/files/` with UUID-based naming
- **Thumbnails**: Auto-generate for images via sharp
- **Quotas**: Configurable per-user and per-group storage limits
- **Cleanup**: Periodic job to remove orphaned files

### 4.2 Client Components

#### 4.2.1 Electron Main Process
- **Window Management**: Main window, call pop-out windows, notification windows
- **Tray Integration**: System tray icon with status and quick actions
- **Auto-Launch**: Optional start-with-Windows via registry
- **IPC Bridge**: Secure IPC between main process and renderer
- **desktopCapturer**: Expose screen/window sources for screen sharing
- **Global Shortcuts**: Mute/unmute, push-to-talk, answer/decline call

#### 4.2.2 UI Modules (React)

| Module | Description |
|--------|-------------|
| `AuthModule` | Login, registration, profile management |
| `ContactsModule` | User list, search, online status indicators |
| `ChatModule` | Message list, input, file attachments, emoji picker, reply threading |
| `CallModule` | Call controls, participant grid, active speaker highlight, pip mode |
| `ScreenShareModule` | Source picker, share controls, viewer panel |
| `SettingsModule` | Audio/video device selection, network info, preferences |
| `NotificationModule` | Toast notifications, incoming call overlay |

#### 4.2.3 Client Services (TypeScript)

| Service | Responsibility |
|---------|---------------|
| `DiscoveryClient` | Find LAN server via mDNS + UDP broadcast listener |
| `AuthClient` | Login, token management, auto-refresh |
| `SocketManager` | Socket.IO connection lifecycle, reconnection, namespace management |
| `WebRTCManager` | P2P connection setup (simple-peer), ICE handling |
| `MediasoupClient` | SFU transport/producer/consumer management (mediasoup-client) |
| `MediaDeviceManager` | Enumerate cameras/mics, handle device changes, manage constraints |
| `ScreenCaptureManager` | Electron desktopCapturer integration, source selection |
| `ChatStore` | Local message cache (SQLite), sync with server |
| `CallStateManager` | Call lifecycle state machine, ringtone management |
| `FileTransferManager` | Upload/download progress, chunked transfers, P2P file via DataChannel |

---

## 5. Complete Folder Structure

```
CommClient/
├── package.json                          # Monorepo root (npm workspaces)
├── turbo.json                            # Turborepo pipeline config
├── tsconfig.base.json                    # Shared TypeScript config
├── .env.example                          # Environment template
├── docker-compose.yml                    # Optional: containerized server deployment
├── CommClient.spec                       # PyInstaller spec (if Python components needed)
│
├── packages/
│   ├── shared/                           # Shared types, constants, utilities
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── src/
│   │       ├── types/
│   │       │   ├── user.ts               # User, Profile, Presence types
│   │       │   ├── message.ts            # Message, Channel, Thread types
│   │       │   ├── call.ts               # Call, Participant, MediaTrack types
│   │       │   ├── signal.ts             # SDP, ICE, Signaling event types
│   │       │   ├── mediasoup.ts          # Transport, Producer, Consumer types
│   │       │   └── api.ts                # REST API request/response types
│   │       ├── constants/
│   │       │   ├── events.ts             # All Socket.IO event names (single source of truth)
│   │       │   ├── media.ts              # Codec configs, constraints, SFU thresholds
│   │       │   └── config.ts             # Default ports, timeouts, limits
│   │       ├── utils/
│   │       │   ├── validation.ts         # Input validation schemas (zod)
│   │       │   ├── crypto.ts             # Encryption helpers for E2E (future)
│   │       │   └── network.ts            # IP range detection, LAN validation
│   │       └── index.ts
│   │
│   ├── server/                           # LAN Server
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   ├── mediasoup-config.ts           # mediasoup Worker/Router settings
│   │   └── src/
│   │       ├── index.ts                  # Server entry point
│   │       ├── app.ts                    # Fastify app setup + plugins
│   │       ├── config/
│   │       │   ├── env.ts                # Environment variable loader
│   │       │   ├── database.ts           # SQLite connection + migrations
│   │       │   └── mediasoup.ts          # Worker pool, codec capabilities
│   │       ├── services/
│   │       │   ├── discovery.service.ts  # mDNS + UDP broadcast
│   │       │   ├── auth.service.ts       # User registration, login, JWT
│   │       │   ├── user.service.ts       # User CRUD, profile, search
│   │       │   ├── chat.service.ts       # Messages, channels, delivery receipts
│   │       │   ├── call.service.ts       # Call state machine, routing decisions
│   │       │   ├── media.service.ts      # mediasoup Router/Transport/Producer/Consumer
│   │       │   ├── presence.service.ts   # Online/offline tracking, heartbeat
│   │       │   ├── file.service.ts       # File upload, storage, thumbnails
│   │       │   └── notification.service.ts # Push to connected clients
│   │       ├── routes/
│   │       │   ├── auth.routes.ts        # POST /auth/register, /auth/login, /auth/refresh
│   │       │   ├── user.routes.ts        # GET /users, /users/:id, PATCH /users/:id
│   │       │   ├── chat.routes.ts        # GET /channels, /channels/:id/messages
│   │       │   ├── file.routes.ts        # POST /files/upload, GET /files/:id
│   │       │   └── health.routes.ts      # GET /health, /info
│   │       ├── socket/
│   │       │   ├── index.ts              # Socket.IO server setup + middleware
│   │       │   ├── auth.middleware.ts     # JWT verification on socket connect
│   │       │   ├── chat.handler.ts       # Chat namespace event handlers
│   │       │   ├── call.handler.ts       # Call namespace event handlers
│   │       │   ├── presence.handler.ts   # Presence namespace event handlers
│   │       │   └── mediasoup.handler.ts  # SFU signaling event handlers
│   │       ├── models/
│   │       │   ├── user.model.ts         # User table schema + queries
│   │       │   ├── message.model.ts      # Message table schema + queries
│   │       │   ├── channel.model.ts      # Channel table schema + queries
│   │       │   └── session.model.ts      # Active session tracking
│   │       ├── migrations/
│   │       │   ├── 001_initial.sql       # Users, channels, messages tables
│   │       │   └── 002_files.sql         # File metadata table
│   │       └── utils/
│   │           ├── logger.ts             # Pino logger setup
│   │           ├── errors.ts             # Custom error classes
│   │           └── lan.ts                # LAN IP detection, interface enumeration
│   │
│   └── client/                           # Electron Desktop App
│       ├── package.json
│       ├── tsconfig.json
│       ├── electron-builder.yml          # Electron-builder config (NSIS installer)
│       ├── tailwind.config.js
│       ├── vite.config.ts                # Vite for renderer process
│       ├── src/
│       │   ├── main/                     # Electron Main Process
│       │   │   ├── index.ts              # App entry, window creation
│       │   │   ├── tray.ts               # System tray setup
│       │   │   ├── ipc-handlers.ts       # IPC bridge handlers
│       │   │   ├── auto-launch.ts        # Start-with-Windows
│       │   │   ├── global-shortcuts.ts   # Keyboard shortcuts
│       │   │   ├── updater.ts            # Auto-update (LAN update server)
│       │   │   └── preload.ts            # Preload script (contextBridge)
│       │   │
│       │   ├── renderer/                 # React UI (Renderer Process)
│       │   │   ├── index.html
│       │   │   ├── main.tsx              # React entry point
│       │   │   ├── App.tsx               # Root component + router
│       │   │   ├── components/
│       │   │   │   ├── layout/
│       │   │   │   │   ├── Sidebar.tsx           # Navigation sidebar
│       │   │   │   │   ├── Header.tsx            # Top bar with search + status
│       │   │   │   │   └── MainLayout.tsx        # Shell layout
│       │   │   │   ├── auth/
│       │   │   │   │   ├── LoginForm.tsx         # Login page
│       │   │   │   │   ├── RegisterForm.tsx      # Registration page
│       │   │   │   │   └── ProfileEditor.tsx     # Profile settings
│       │   │   │   ├── contacts/
│       │   │   │   │   ├── ContactList.tsx       # User list with presence
│       │   │   │   │   ├── ContactCard.tsx       # User card with actions
│       │   │   │   │   └── ContactSearch.tsx     # Search/filter users
│       │   │   │   ├── chat/
│       │   │   │   │   ├── ChatView.tsx          # Main chat container
│       │   │   │   │   ├── MessageList.tsx       # Virtualized message list
│       │   │   │   │   ├── MessageBubble.tsx     # Single message component
│       │   │   │   │   ├── MessageInput.tsx      # Rich input with attachments
│       │   │   │   │   ├── ChannelList.tsx       # Channel/conversation sidebar
│       │   │   │   │   ├── ChannelHeader.tsx     # Channel info + call button
│       │   │   │   │   ├── FilePreview.tsx       # Inline file/image preview
│       │   │   │   │   └── EmojiPicker.tsx       # Emoji selection
│       │   │   │   ├── call/
│       │   │   │   │   ├── CallView.tsx          # Active call container
│       │   │   │   │   ├── CallControls.tsx      # Mute, camera, share, hang up
│       │   │   │   │   ├── ParticipantGrid.tsx   # Video grid layout
│       │   │   │   │   ├── ParticipantTile.tsx   # Single participant video + name
│       │   │   │   │   ├── IncomingCall.tsx      # Incoming call overlay
│       │   │   │   │   ├── CallTimer.tsx         # Call duration display
│       │   │   │   │   ├── ActiveSpeaker.tsx     # Speaker highlight indicator
│       │   │   │   │   └── PipWindow.tsx         # Picture-in-picture mode
│       │   │   │   ├── screen-share/
│       │   │   │   │   ├── SourcePicker.tsx      # Screen/window selector
│       │   │   │   │   ├── ShareControls.tsx     # Stop sharing, pause
│       │   │   │   │   └── ShareViewer.tsx       # Fullscreen viewer for received shares
│       │   │   │   ├── settings/
│       │   │   │   │   ├── SettingsView.tsx      # Settings container
│       │   │   │   │   ├── AudioSettings.tsx     # Mic/speaker selection + test
│       │   │   │   │   ├── VideoSettings.tsx     # Camera selection + preview
│       │   │   │   │   ├── NetworkInfo.tsx       # Server connection, LAN info
│       │   │   │   │   └── AppearanceSettings.tsx # Theme, language
│       │   │   │   └── common/
│       │   │   │       ├── Avatar.tsx            # User avatar
│       │   │   │       ├── Badge.tsx             # Status badge, notification count
│       │   │   │       ├── Modal.tsx             # Reusable modal
│       │   │   │       ├── Toast.tsx             # Toast notifications
│       │   │   │       └── LoadingSpinner.tsx    # Loading indicator
│       │   │   │
│       │   │   ├── services/
│       │   │   │   ├── discovery.client.ts       # mDNS + UDP server discovery
│       │   │   │   ├── auth.client.ts            # Login, token refresh, logout
│       │   │   │   ├── socket.manager.ts         # Socket.IO connection management
│       │   │   │   ├── webrtc.manager.ts         # P2P connections (simple-peer)
│       │   │   │   ├── mediasoup.client.ts       # SFU client (mediasoup-client)
│       │   │   │   ├── media-device.manager.ts   # Camera/mic enumeration
│       │   │   │   ├── screen-capture.manager.ts # Electron desktopCapturer
│       │   │   │   ├── chat.store.ts             # Local SQLite + server sync
│       │   │   │   ├── call-state.manager.ts     # Call lifecycle FSM
│       │   │   │   ├── file-transfer.manager.ts  # Upload/download + P2P file
│       │   │   │   └── notification.manager.ts   # OS-level notifications
│       │   │   │
│       │   │   ├── stores/
│       │   │   │   ├── auth.store.ts             # User session state
│       │   │   │   ├── contacts.store.ts         # User list + presence
│       │   │   │   ├── chat.store.ts             # Messages, channels, unread counts
│       │   │   │   ├── call.store.ts             # Active call state, participants
│       │   │   │   └── settings.store.ts         # User preferences
│       │   │   │
│       │   │   ├── hooks/
│       │   │   │   ├── useSocket.ts              # Socket connection hook
│       │   │   │   ├── useWebRTC.ts              # P2P call hook
│       │   │   │   ├── useMediasoup.ts           # SFU call hook
│       │   │   │   ├── useMediaDevices.ts        # Camera/mic management hook
│       │   │   │   ├── useScreenShare.ts         # Screen sharing hook
│       │   │   │   └── useChat.ts                # Chat operations hook
│       │   │   │
│       │   │   └── styles/
│       │   │       ├── globals.css               # Tailwind imports + custom vars
│       │   │       └── themes/
│       │   │           ├── dark.css              # Dark theme variables
│       │   │           └── light.css             # Light theme variables
│       │   │
│       │   └── assets/
│       │       ├── icons/                        # App icons (tray, dock)
│       │       ├── sounds/                       # Ring, notification sounds
│       │       └── images/                       # Default avatars, logos
│       │
│       └── resources/
│           └── installer/                        # NSIS installer assets
│               ├── icon.ico
│               └── banner.bmp
│
├── scripts/
│   ├── dev.sh                            # Start server + client in dev mode
│   ├── build-server.sh                   # Build server for production
│   ├── build-client.sh                   # Build Electron app
│   ├── build-all.sh                      # Full build pipeline
│   └── install-server.bat                # Windows service installer for server
│
├── docs/
│   ├── DEPLOYMENT.md                     # Deployment guide
│   ├── API.md                            # REST API documentation
│   ├── SOCKET-EVENTS.md                  # Socket.IO event reference
│   └── ARCHITECTURE.md                   # This document (detailed version)
│
└── data/                                 # Server runtime data (gitignored)
    ├── commclient.db                     # SQLite database
    └── files/                            # Uploaded files
```

---

## 6. Data Flows

### 6.1 Server Discovery Flow

```
1. Server starts → advertises "_commclient._tcp" via mDNS on port 3000
2. Server starts → sends UDP broadcast to 255.255.255.255:41234 every 3 seconds
   Payload: { type: "commclient-server", port: 3000, version: "1.0.0" }
3. Client starts → queries mDNS for "_commclient._tcp"
   - If found: resolves server IP + port
   - If not found within 3s: falls back to UDP broadcast listener on port 41234
4. Client connects to http://<server-ip>:3000
5. Client validates server via GET /health
6. Connection established → proceed to auth
```

### 6.2 Authentication Flow

```
POST /auth/register
  Request:  { username, displayName, password, avatar? }
  Response: { user: { id, username, displayName }, token, refreshToken }

POST /auth/login
  Request:  { username, password }
  Response: { user: { id, username, displayName, avatar }, token, refreshToken }

POST /auth/refresh
  Request:  { refreshToken }
  Response: { token, refreshToken }

After login:
  1. Client stores tokens in encrypted local storage (safeStorage API)
  2. Client connects Socket.IO with auth: { token } in handshake
  3. Server validates JWT on socket connect middleware
  4. Server adds user to presence tracking
  5. Server broadcasts presence:user-online to all connected clients
```

### 6.3 Text Chat Flow

#### Private (1-to-1) Message

```
Sender                     Server                      Receiver
  │                          │                            │
  ├── chat:send-message ────►│  (store in SQLite)         │
  │   { to, content, type }  │                            │
  │                          ├── chat:new-message ───────►│
  │                          │                            │
  │                          │◄── chat:message-delivered ─┤
  │◄── chat:delivery-receipt │   { messageId, status }    │
  │   { messageId: delivered}│                            │
  │                          │                            │
  │                          │◄── chat:message-read ──────┤
  │◄── chat:delivery-receipt │   { messageId }            │
  │   { messageId: read }    │                            │
```

#### Group Message

```
Sender                     Server                     Members (N)
  │                          │                            │
  ├── chat:send-message ────►│  (store + fan out)         │
  │   { channelId, content } │                            │
  │                          ├── chat:new-message ───────►│ (to all channel members)
  │                          │                            │
  │◄── chat:message-ack ────┤  { messageId, timestamp }  │
```

#### Message History Sync

```
GET /channels/:channelId/messages?before=<timestamp>&limit=50
  Response: { messages: [...], hasMore: boolean }
```

### 6.4 1-to-1 Call Flow (P2P)

```
Caller                     Signaling Server              Callee
  │                              │                          │
  │  getUserMedia()              │                          │
  ├── call:initiate ────────────►│                          │
  │   { calleeId, mediaType }   ├── call:incoming ────────►│
  │                              │   { callerId, mediaType }│
  │                              │                          │ getUserMedia()
  │                              │◄── call:accept ─────────┤
  │◄── call:peer-ready ─────────┤                          │
  │                              │                          │
  │   create simple-peer(initiator=true)                    │
  ├── signal:offer ─────────────►├── signal:offer ─────────►│
  │                              │                          │ create simple-peer(initiator=false)
  │◄── signal:answer ────────────┤◄── signal:answer ───────┤
  │◄── signal:ice ───────────────┤◄── signal:ice ──────────┤
  │── signal:ice ───────────────►├── signal:ice ───────────►│
  │                              │                          │
  │ ═══════ Direct P2P connection established ═══════════ │
  │          (media bypasses server entirely)               │
  │                              │                          │
  │── call:hangup ──────────────►├── call:ended ───────────►│
```

### 6.5 Group Call Flow (SFU via mediasoup)

```
User A joins group call:

1. A → Server: call:join-group { channelId }
2. Server: creates/gets mediasoup Router for this room
3. Server → A: call:router-capabilities { rtpCapabilities }
4. A: mediasoupClient.Device.load(rtpCapabilities)
5. A → Server: call:create-transport { direction: "send" }
6. Server: router.createWebRtcTransport() → returns { id, iceParameters, dtlsParameters }
7. Server → A: call:transport-created { transportParams }
8. A: device.createSendTransport(transportParams)
9. A → Server: call:create-transport { direction: "recv" }
10. Server → A: call:transport-created { transportParams }
11. A: device.createRecvTransport(transportParams)

Producing media:
12. A: sendTransport.produce({ track: audioTrack })
    → triggers "produce" event on transport
13. A → Server: call:produce { transportId, kind: "audio", rtpParameters }
14. Server: transport.produce(rtpParameters) → producerId
15. Server → A: call:produced { producerId }
16. Server → B,C,D: call:new-producer { peerId: A, producerId, kind: "audio" }

Consuming media (B receives A's audio):
17. B → Server: call:consume { producerId }
18. Server: transport.consume({ producerId, rtpCapabilities }) → { consumerId, rtpParameters }
19. Server → B: call:consumed { consumerId, producerId, kind, rtpParameters }
20. B: recvTransport.consume(consumerParams) → track
21. B → Server: call:consumer-resume { consumerId }
```

### 6.6 Screen Share Flow

#### 1-to-1 (P2P DataChannel + Track)

```
Sharer                                                    Viewer
  │                                                          │
  │  Electron: desktopCapturer.getSources()                  │
  │  User selects source → getDisplayMedia(sourceId)         │
  │                                                          │
  │── peer.addTrack(screenTrack) ───────────────────────────►│
  │   (renegotiation via existing P2P connection)            │
  │                                                          │ renderScreenTrack()
  │                                                          │
  │── signal:screen-share-start ────────(via signaling)─────►│
  │                                                          │
  │  (user stops sharing)                                    │
  │── peer.removeTrack(screenTrack) ────────────────────────►│
  │── signal:screen-share-stop ─────────(via signaling)─────►│
```

#### Group (SFU)

```
Sharer                     mediasoup SFU                  Viewers
  │                              │                          │
  │  desktopCapturer → track     │                          │
  │── call:produce ─────────────►│                          │
  │   { kind: "video",          │                          │
  │     appData: { share: true }}│                          │
  │                              │                          │
  │                              ├── call:new-producer ────►│
  │                              │   { appData.share: true }│
  │                              │                          │
  │                              │◄── call:consume ─────────┤
  │                              ├── call:consumed ────────►│
  │                              │   (screen share track)   │
```

---

## 7. API Reference

### 7.1 REST API Endpoints

#### Auth
```
POST   /api/auth/register          # Create new user account
POST   /api/auth/login             # Login, receive JWT pair
POST   /api/auth/refresh           # Refresh access token
POST   /api/auth/logout            # Invalidate refresh token
```

#### Users
```
GET    /api/users                  # List all users (paginated)
GET    /api/users/search?q=       # Search users by name/username
GET    /api/users/:id              # Get user profile
PATCH  /api/users/:id              # Update own profile
POST   /api/users/:id/avatar      # Upload avatar image
```

#### Channels
```
POST   /api/channels               # Create channel (group or DM)
GET    /api/channels               # List user's channels
GET    /api/channels/:id           # Get channel details + members
PATCH  /api/channels/:id           # Update channel (name, topic)
POST   /api/channels/:id/members   # Add member to group channel
DELETE /api/channels/:id/members/:uid # Remove member
```

#### Messages
```
GET    /api/channels/:id/messages  # Get messages (paginated, cursor-based)
       ?before=<timestamp>&limit=50
GET    /api/channels/:id/messages/search?q=  # Full-text search
DELETE /api/messages/:id           # Delete own message
```

#### Files
```
POST   /api/files/upload           # Upload file (multipart)
GET    /api/files/:id              # Download file
GET    /api/files/:id/thumbnail    # Get image thumbnail
DELETE /api/files/:id              # Delete own file
```

#### System
```
GET    /api/health                 # Server health check
GET    /api/info                   # Server version, uptime, connected users count
```

### 7.2 Socket.IO Events

#### Namespace: `/` (default — presence + signaling)

**Client → Server:**

| Event | Payload | Description |
|-------|---------|-------------|
| `presence:heartbeat` | `{}` | Keep-alive signal (every 5s) |
| `presence:status` | `{ status: 'online' \| 'away' \| 'busy' \| 'dnd' }` | Set user status |
| `call:initiate` | `{ calleeId, mediaType: 'audio' \| 'video' }` | Start 1-to-1 call |
| `call:accept` | `{ callId }` | Accept incoming call |
| `call:reject` | `{ callId, reason? }` | Reject incoming call |
| `call:hangup` | `{ callId }` | End active call |
| `call:join-group` | `{ channelId, mediaType }` | Join group call room |
| `call:leave-group` | `{ channelId }` | Leave group call room |
| `signal:offer` | `{ targetId, sdp }` | WebRTC SDP offer (P2P) |
| `signal:answer` | `{ targetId, sdp }` | WebRTC SDP answer (P2P) |
| `signal:ice-candidate` | `{ targetId, candidate }` | ICE candidate (P2P) |

**Server → Client:**

| Event | Payload | Description |
|-------|---------|-------------|
| `presence:user-online` | `{ userId, status }` | User came online |
| `presence:user-offline` | `{ userId }` | User went offline |
| `presence:user-status` | `{ userId, status }` | User status changed |
| `call:incoming` | `{ callId, callerId, callerName, mediaType }` | Incoming call ring |
| `call:accepted` | `{ callId }` | Callee accepted your call |
| `call:rejected` | `{ callId, reason? }` | Callee rejected your call |
| `call:ended` | `{ callId, reason }` | Call ended by other party |
| `call:peer-ready` | `{ callId, peerId }` | Peer ready for signaling |
| `signal:offer` | `{ fromId, sdp }` | Forwarded SDP offer |
| `signal:answer` | `{ fromId, sdp }` | Forwarded SDP answer |
| `signal:ice-candidate` | `{ fromId, candidate }` | Forwarded ICE candidate |

#### Namespace: `/chat`

**Client → Server:**

| Event | Payload | Description |
|-------|---------|-------------|
| `chat:send-message` | `{ channelId, content, type, replyTo? }` | Send message |
| `chat:typing-start` | `{ channelId }` | User started typing |
| `chat:typing-stop` | `{ channelId }` | User stopped typing |
| `chat:message-read` | `{ channelId, messageId }` | Mark message as read |
| `chat:reaction` | `{ messageId, emoji }` | Add/toggle reaction |

**Server → Client:**

| Event | Payload | Description |
|-------|---------|-------------|
| `chat:new-message` | `{ message: Message }` | New message received |
| `chat:typing` | `{ channelId, userId, isTyping }` | Typing indicator |
| `chat:delivery-receipt` | `{ messageId, status, userId }` | Delivery status update |
| `chat:message-deleted` | `{ messageId }` | Message was deleted |
| `chat:reaction-update` | `{ messageId, reactions }` | Reaction changed |

#### Namespace: `/media` (SFU signaling)

**Client → Server:**

| Event | Payload | Description |
|-------|---------|-------------|
| `media:get-capabilities` | `{ roomId }` | Get Router RTP capabilities |
| `media:create-transport` | `{ roomId, direction: 'send' \| 'recv' }` | Create WebRTC transport |
| `media:connect-transport` | `{ transportId, dtlsParameters }` | Complete transport handshake |
| `media:produce` | `{ transportId, kind, rtpParameters, appData }` | Start producing media |
| `media:consume` | `{ producerId, rtpCapabilities }` | Request to consume a producer |
| `media:consumer-resume` | `{ consumerId }` | Resume paused consumer |
| `media:producer-pause` | `{ producerId }` | Pause producing (mute) |
| `media:producer-resume` | `{ producerId }` | Resume producing (unmute) |
| `media:producer-close` | `{ producerId }` | Stop producing |

**Server → Client:**

| Event | Payload | Description |
|-------|---------|-------------|
| `media:capabilities` | `{ rtpCapabilities }` | Router codecs |
| `media:transport-created` | `{ id, iceParameters, iceCandidates, dtlsParameters }` | Transport params |
| `media:produced` | `{ producerId }` | Producer created successfully |
| `media:new-producer` | `{ peerId, producerId, kind, appData }` | New producer in room |
| `media:producer-removed` | `{ peerId, producerId }` | Producer left |
| `media:consumed` | `{ consumerId, producerId, kind, rtpParameters }` | Consumer created |
| `media:active-speaker` | `{ peerId }` | Current dominant speaker |
| `media:peer-joined` | `{ peerId, displayName }` | New peer in room |
| `media:peer-left` | `{ peerId }` | Peer left room |

---

## 8. Database Schema (SQLite)

```sql
-- Users
CREATE TABLE users (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    username    TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    avatar_url  TEXT,
    status      TEXT DEFAULT 'offline' CHECK(status IN ('online','offline','away','busy','dnd')),
    last_seen   DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Channels (DM or Group)
CREATE TABLE channels (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    type        TEXT NOT NULL CHECK(type IN ('dm', 'group')),
    name        TEXT,           -- NULL for DMs
    topic       TEXT,
    created_by  TEXT REFERENCES users(id),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Channel Members
CREATE TABLE channel_members (
    channel_id  TEXT REFERENCES channels(id) ON DELETE CASCADE,
    user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT DEFAULT 'member' CHECK(role IN ('admin', 'member')),
    joined_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_read   DATETIME,
    PRIMARY KEY (channel_id, user_id)
);

-- Messages
CREATE TABLE messages (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    channel_id  TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    sender_id   TEXT NOT NULL REFERENCES users(id),
    content     TEXT NOT NULL,
    type        TEXT DEFAULT 'text' CHECK(type IN ('text','file','system','reply')),
    reply_to    TEXT REFERENCES messages(id),
    status      TEXT DEFAULT 'sent' CHECK(status IN ('sent','delivered','read')),
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    deleted_at  DATETIME
);

-- Full-text search on messages
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content=messages, content_rowid=rowid);

-- Message Reactions
CREATE TABLE reactions (
    message_id  TEXT REFERENCES messages(id) ON DELETE CASCADE,
    user_id     TEXT REFERENCES users(id) ON DELETE CASCADE,
    emoji       TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (message_id, user_id, emoji)
);

-- File Metadata
CREATE TABLE files (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    uploader_id TEXT NOT NULL REFERENCES users(id),
    channel_id  TEXT REFERENCES channels(id),
    message_id  TEXT REFERENCES messages(id),
    filename    TEXT NOT NULL,
    mime_type   TEXT NOT NULL,
    size        INTEGER NOT NULL,
    path        TEXT NOT NULL,
    thumbnail   TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Active Sessions (for token management)
CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,
    ip_address  TEXT,
    user_agent  TEXT,
    expires_at  DATETIME NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX idx_messages_channel ON messages(channel_id, created_at DESC);
CREATE INDEX idx_messages_sender ON messages(sender_id);
CREATE INDEX idx_channel_members_user ON channel_members(user_id);
CREATE INDEX idx_files_channel ON files(channel_id);
CREATE INDEX idx_sessions_user ON sessions(user_id);
```

---

## 9. LAN/WiFi-Only Deployment Model

### 9.1 Deployment Architecture

```
Office / Home LAN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
                    ┌─────────────────────────┐
                    │   Dedicated Server PC   │
                    │   (or any PC on LAN)    │
                    │                         │
                    │   ┌─────────────────┐   │
                    │   │  CommClient      │   │
                    │   │  Server          │   │
                    │   │  (Node.js)       │   │
                    │   │                  │   │
                    │   │  Port 3000: HTTP │   │
                    │   │  + Socket.IO     │   │
                    │   │  + mediasoup     │   │
                    │   │                  │   │
                    │   │  Port 41234: UDP │   │
                    │   │  (discovery)     │   │
                    │   │                  │   │
                    │   │  Port 40000-49999│   │
                    │   │  (mediasoup RTP) │   │
                    │   └─────────────────┘   │
                    │                         │
                    │   Data: ./data/         │
                    │   commclient.db         │
                    │   files/                │
                    └───────────┬─────────────┘
                                │
                ┌───────────────┼───────────────┐
                │               │               │
         ┌──────▼─────┐  ┌─────▼──────┐  ┌─────▼──────┐
         │  Client PC  │  │  Client PC  │  │  Client PC  │
         │  (Windows)  │  │  (Windows)  │  │  (Windows)  │
         │             │  │             │  │             │
         │ CommClient  │  │ CommClient  │  │ CommClient  │
         │  Desktop    │  │  Desktop    │  │  Desktop    │
         │  (Electron) │  │  (Electron) │  │  (Electron) │
         └─────────────┘  └────────────┘  └────────────┘
```

### 9.2 Server Deployment Options

#### Option A: Dedicated Server Machine (Recommended for offices)
- Install Node.js 20 LTS on a dedicated Windows/Linux PC
- Run `npm run start:server` as a Windows Service (via node-windows or NSSM)
- Assign a static LAN IP (e.g., 192.168.1.100)
- Open firewall ports: 3000 (TCP), 41234 (UDP), 40000-49999 (UDP)

#### Option B: Embedded in Client (Small teams, 2-5 users)
- The first user to launch CommClient can opt to run as "host"
- Server starts embedded within their Electron process
- Other clients discover this host via mDNS
- If the host disconnects, another client can take over (with data migration)

#### Option C: Docker Container (IT-managed deployment)
```yaml
# docker-compose.yml
version: '3.8'
services:
  commclient-server:
    build: ./packages/server
    network_mode: host           # Required for mDNS and UDP broadcast
    volumes:
      - ./data:/app/data         # Persist database + files
    environment:
      - PORT=3000
      - MEDIASOUP_MIN_PORT=40000
      - MEDIASOUP_MAX_PORT=49999
      - JWT_SECRET=<generate-random-secret>
    restart: unless-stopped
```

### 9.3 Client Deployment

1. Build: `cd packages/client && npm run build && npx electron-builder --win`
2. Output: `dist/CommClient-Setup-1.0.0.exe` (NSIS installer)
3. Distribute via LAN shared folder or USB
4. User installs → app starts → auto-discovers server → login/register

### 9.4 Firewall Rules (Windows)

```powershell
# Server machine
netsh advfirewall firewall add rule name="CommClient HTTP" dir=in action=allow protocol=tcp localport=3000
netsh advfirewall firewall add rule name="CommClient Discovery" dir=in action=allow protocol=udp localport=41234
netsh advfirewall firewall add rule name="CommClient Media" dir=in action=allow protocol=udp localport=40000-49999
netsh advfirewall firewall add rule name="CommClient mDNS" dir=in action=allow protocol=udp localport=5353
```

### 9.5 Network Requirements

| Metric | Minimum | Recommended |
|--------|---------|-------------|
| LAN Speed | 100 Mbps | 1 Gbps |
| Latency | < 10ms | < 2ms |
| WiFi Standard | 802.11n | 802.11ac/ax |
| Server RAM | 2 GB | 4-8 GB |
| Server CPU | 2 cores | 4+ cores (mediasoup uses 1 worker per core) |
| Disk | 10 GB | 50+ GB (for file storage) |
| Max Concurrent Users | 20 | 100+ |

---

## 10. mediasoup Configuration

```typescript
// packages/server/mediasoup-config.ts
import os from 'os';
import { types as mediasoupTypes } from 'mediasoup';

export const mediasoupConfig = {
  // One worker per CPU core
  numWorkers: os.cpus().length,

  worker: {
    rtcMinPort: 40000,
    rtcMaxPort: 49999,
    logLevel: 'warn' as mediasoupTypes.WorkerLogLevel,
    logTags: ['info', 'ice', 'dtls', 'rtp', 'srtp'] as mediasoupTypes.WorkerLogTag[],
  },

  router: {
    mediaCodecs: [
      {
        kind: 'audio' as mediasoupTypes.MediaKind,
        mimeType: 'audio/opus',
        clockRate: 48000,
        channels: 2,
      },
      {
        kind: 'video' as mediasoupTypes.MediaKind,
        mimeType: 'video/VP8',
        clockRate: 90000,
        parameters: {
          'x-google-start-bitrate': 1000,
        },
      },
      {
        kind: 'video' as mediasoupTypes.MediaKind,
        mimeType: 'video/VP9',
        clockRate: 90000,
        parameters: {
          'profile-id': 2,
          'x-google-start-bitrate': 1000,
        },
      },
      {
        kind: 'video' as mediasoupTypes.MediaKind,
        mimeType: 'video/h264',
        clockRate: 90000,
        parameters: {
          'packetization-mode': 1,
          'profile-level-id': '4d0032',
          'level-asymmetry-allowed': 1,
          'x-google-start-bitrate': 1000,
        },
      },
    ] as mediasoupTypes.RtpCodecCapability[],
  },

  webRtcTransport: {
    listenIps: [
      {
        ip: '0.0.0.0',        // Listen on all interfaces
        announcedIp: undefined, // Set dynamically to server's LAN IP
      },
    ],
    maxIncomingBitrate: 10_000_000,  // 10 Mbps per transport (generous for LAN)
    initialAvailableOutgoingBitrate: 5_000_000, // 5 Mbps initial
    minimumAvailableOutgoingBitrate: 600_000,
    enableUdp: true,
    enableTcp: true,
    preferUdp: true,
  },
};
```

---

## 11. MVP vs Production Phases

### Phase 1: MVP (Weeks 1-6)

**Goal**: Core communication working on LAN with manual server IP entry.

| Week | Deliverable |
|------|-------------|
| 1 | Project setup, monorepo, Electron shell, Fastify server, SQLite schema, basic UI shell |
| 2 | User registration/login (local auth), JWT, Socket.IO connection, presence (online/offline) |
| 3 | 1-to-1 text chat (send/receive, history, delivery receipts), channel creation |
| 4 | 1-to-1 audio/video call (P2P via simple-peer), call UI (ring, accept, reject, hangup) |
| 5 | Group text chat (channels, member management), typing indicators |
| 6 | Group audio/video call (mediasoup SFU), participant grid, mute/unmute |

**MVP Scope:**
- Manual server IP entry (no auto-discovery yet)
- 1-to-1 P2P calls (audio + video)
- Group SFU calls (audio + video, up to 8 participants)
- Text messaging (1-to-1 and group)
- Basic contact list with online/offline
- Windows Electron app with clean UI
- SQLite persistence for messages and users

**MVP NOT included:**
- mDNS auto-discovery
- Screen sharing
- File transfer
- Reactions/threading
- Dark/light theme toggle
- Auto-update

### Phase 2: Production (Weeks 7-14)

| Week | Deliverable |
|------|-------------|
| 7 | mDNS + UDP auto-discovery, zero-config server finding |
| 8 | Screen sharing (1-to-1 P2P + group SFU), source picker UI |
| 9 | File transfer (upload/download via HTTP, thumbnails, inline preview) |
| 10 | P2P file transfer via DataChannel, message search (FTS5) |
| 11 | Active speaker detection, simulcast, bandwidth adaptation, last-N for large groups |
| 12 | Push-to-talk, global shortcuts, system tray, auto-launch, notification sounds |
| 13 | Dark/light theme, user profiles, message reactions, reply threading |
| 14 | Windows installer (NSIS), LAN auto-update, deployment docs, testing |

### Phase 3: Enterprise (Post-launch)

- End-to-end encryption (E2EE) for 1-to-1 using Double Ratchet
- LDAP/Active Directory integration for auth
- Role-based access control (admin, moderator, user)
- Server clustering (multiple mediasoup nodes for 100+ concurrent users)
- Recording (server-side or client-side)
- Whiteboard / collaborative annotations
- Mobile companion app (React Native)
- Audit logging
- Bandwidth monitoring dashboard

---

## 12. Key Configuration Files

### 12.1 Root package.json (Monorepo)

```json
{
  "name": "commclient",
  "private": true,
  "workspaces": ["packages/*"],
  "scripts": {
    "dev": "turbo run dev",
    "build": "turbo run build",
    "dev:server": "npm run dev -w packages/server",
    "dev:client": "npm run dev -w packages/client",
    "build:server": "npm run build -w packages/server",
    "build:client": "npm run build -w packages/client",
    "build:installer": "npm run build:client && cd packages/client && npx electron-builder --win"
  },
  "devDependencies": {
    "turbo": "^2.0.0",
    "typescript": "^5.4.0"
  }
}
```

### 12.2 Server Dependencies

```json
{
  "dependencies": {
    "fastify": "^4.26.0",
    "@fastify/cors": "^9.0.0",
    "@fastify/multipart": "^8.0.0",
    "@fastify/static": "^7.0.0",
    "socket.io": "^4.7.0",
    "mediasoup": "^3.13.0",
    "better-sqlite3": "^11.0.0",
    "bcryptjs": "^2.4.3",
    "jsonwebtoken": "^9.0.0",
    "bonjour-service": "^1.2.0",
    "dgram": "builtin",
    "sharp": "^0.33.0",
    "pino": "^8.0.0",
    "zod": "^3.22.0",
    "uuid": "^9.0.0"
  }
}
```

### 12.3 Client Dependencies

```json
{
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "react-router-dom": "^6.22.0",
    "zustand": "^4.5.0",
    "socket.io-client": "^4.7.0",
    "simple-peer": "^9.11.0",
    "mediasoup-client": "^3.7.0",
    "better-sqlite3": "^11.0.0",
    "@electron/remote": "^2.1.0",
    "bonjour-service": "^1.2.0",
    "lucide-react": "^0.383.0",
    "date-fns": "^3.6.0",
    "clsx": "^2.1.0"
  },
  "devDependencies": {
    "electron": "^30.0.0",
    "electron-builder": "^24.0.0",
    "vite": "^5.2.0",
    "@vitejs/plugin-react": "^4.2.0",
    "tailwindcss": "^3.4.0",
    "autoprefixer": "^10.4.0",
    "postcss": "^8.4.0"
  }
}
```

### 12.4 Electron Builder Config

```yaml
# packages/client/electron-builder.yml
appId: com.commclient.app
productName: CommClient
directories:
  output: dist
  buildResources: resources
files:
  - dist-electron/**/*
  - dist-renderer/**/*
win:
  target:
    - target: nsis
      arch: [x64]
  icon: resources/installer/icon.ico
nsis:
  oneClick: false
  perMachine: true
  allowToChangeInstallationDirectory: true
  installerIcon: resources/installer/icon.ico
  installerSidebar: resources/installer/banner.bmp
  shortcutName: CommClient
```

---

## 13. Security Considerations (LAN Context)

Even on a private LAN, implement these baselines:

1. **Password hashing**: bcrypt with cost factor 12
2. **JWT tokens**: Short-lived (1h access, 7d refresh), signed with HS256 + server-generated secret
3. **DTLS for media**: All WebRTC media is encrypted via DTLS-SRTP by default (mediasoup enforces this)
4. **Socket.IO auth middleware**: Validate JWT on every socket connection handshake
5. **Input validation**: Zod schemas on all API inputs
6. **SQL injection prevention**: Parameterized queries via better-sqlite3 (never string interpolation)
7. **Rate limiting**: Fastify rate limiter on auth endpoints (prevent brute force)
8. **File upload validation**: MIME type checking, size limits, filename sanitization
9. **CORS**: Restrict to LAN IP ranges only (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
10. **Future**: E2EE with Signal Protocol for private messages (Phase 3)

---

## 14. Summary

This blueprint defines a production-grade, LAN-only communication platform with:

- **Hybrid media architecture**: P2P for 1-to-1 (lowest latency), SFU for groups (scalable)
- **Zero-config discovery**: mDNS + UDP broadcast, no manual server entry in production
- **Full feature set**: Audio, video, text, screen share — private and group
- **Clean separation**: Shared types package, server package, client package
- **43 REST endpoints and 40+ Socket.IO events** fully specified
- **SQLite persistence** with full-text search
- **Windows-first Electron desktop app** with native integration
- **3-phase roadmap**: MVP (6 weeks) → Production (14 weeks) → Enterprise

The system is designed for **offices, homes, schools, or any isolated network** where Internet-dependent tools like Zoom/Teams/Slack are not viable or desired.
