# CommClient — Final Project Structure

## Overview
**CommClient** is a production-grade, LAN/WiFi-only desktop communication platform for Windows.
Zero Internet dependency. All traffic stays on local network.

- **Server**: 6,466 lines Python (FastAPI + Socket.IO + SQLAlchemy)
- **Desktop**: 13,814 lines TypeScript/React (Electron + Vite + Zustand + WebRTC)
- **Total**: ~20,280 lines of production code

---

## Directory Tree

```
CommClient/
├── start-commclient.bat              # Master launcher (BAT)
├── start-commclient.ps1              # Master launcher (PowerShell)
├── PROJECT-STRUCTURE.md              # This file
│
├── CommClient-Server/                # Python Backend
│   ├── .env                          # Server configuration
│   ├── requirements.txt              # Python dependencies
│   ├── run.py                        # Server entry point
│   ├── scripts/
│   │   └── start-server.bat          # Server launcher
│   ├── data/                         # Runtime data (auto-created)
│   │   ├── commclient.db             # SQLite database
│   │   └── files/                    # Uploaded files
│   │
│   └── app/
│       ├── main.py                   # FastAPI + Socket.IO ASGI app
│       │
│       ├── core/                     # Framework core
│       │   ├── config.py             # Pydantic settings from .env
│       │   ├── deps.py               # Dependency injection
│       │   ├── exceptions.py         # Error handlers
│       │   ├── logging.py            # Structlog setup
│       │   └── security.py           # JWT + bcrypt auth
│       │
│       ├── db/                       # Database layer
│       │   ├── base.py               # SQLAlchemy Base
│       │   └── session.py            # Async engine + SessionLocal
│       │
│       ├── models/                   # SQLAlchemy ORM models
│       │   ├── user.py               # User account
│       │   ├── session.py            # Auth sessions
│       │   ├── contact.py            # Contact list
│       │   ├── channel.py            # DM + group channels
│       │   ├── message.py            # Messages
│       │   ├── message_status.py     # Delivery/read receipts
│       │   ├── call_log.py           # Call history
│       │   └── file.py              # File metadata
│       │
│       ├── schemas/                  # Pydantic request/response
│       │   ├── auth.py               # Login/register schemas
│       │   ├── user.py               # User CRUD schemas
│       │   ├── channel.py            # Channel schemas
│       │   ├── message.py            # Message schemas
│       │   ├── call.py               # Call schemas
│       │   ├── file.py               # File upload schemas
│       │   └── session.py            # Session schemas
│       │
│       ├── api/routes/               # REST API endpoints
│       │   ├── auth.py               # POST /auth/login, /register, /refresh
│       │   ├── users.py              # GET/PUT /users, /users/me
│       │   ├── channels.py           # CRUD /channels, /channels/:id/members
│       │   ├── messages.py           # CRUD /channels/:id/messages
│       │   ├── calls.py              # GET /calls/history
│       │   ├── files.py              # POST /files/upload, GET /files/:id
│       │   ├── sessions.py           # GET/DELETE /sessions
│       │   └── health.py             # GET /health, /discovery
│       │
│       ├── services/                 # Business logic layer
│       │   ├── auth_service.py       # Authentication logic
│       │   ├── user_service.py       # User management
│       │   ├── channel_service.py    # Channel CRUD + membership
│       │   ├── message_service.py    # Message CRUD
│       │   ├── call_service.py       # Call state (in-memory)
│       │   ├── presence_service.py   # Online/offline tracking (in-memory)
│       │   ├── presenter_service.py  # Screen share presenter lock (in-memory)
│       │   ├── sync_service.py       # Delivery receipts + reconnection sync
│       │   ├── file_service.py       # File storage
│       │   ├── session_service.py    # Session management
│       │   └── discovery_service.py  # UDP broadcast + mDNS (zeroconf)
│       │
│       ├── socket/                   # Socket.IO real-time handlers
│       │   ├── server.py             # AsyncServer + auth middleware
│       │   ├── presence_handlers.py  # Online/offline/status events
│       │   ├── chat_handlers.py      # v1 message events
│       │   ├── call_handlers.py      # v1 + v2 call signaling events
│       │   ├── sync_handlers.py      # v2 messaging: send/deliver/read/typing/reactions
│       │   └── screen_handlers.py    # Presenter lock: request/release/force-stop/queue
│       │
│       └── utils/
│           └── network.py            # LAN IP detection
│
├── CommClient-Desktop/               # Electron + React Frontend
│   ├── package.json                  # Dependencies + build config
│   ├── vite.config.ts                # Vite + Electron plugin config
│   ├── tsconfig.json                 # TypeScript config
│   ├── tailwind.config.js            # Tailwind CSS config
│   ├── scripts/
│   │   ├── start-desktop.bat         # Desktop dev launcher
│   │   └── build.bat                 # Production build + NSIS installer
│   │
│   ├── src/main/
│   │   └── index.ts                  # Electron main process
│   │
│   ├── src/preload/
│   │   └── index.ts                  # Context bridge (IPC)
│   │
│   └── src/renderer/
│       ├── main.tsx                  # React entry point
│       ├── App.tsx                   # v1 root component
│       ├── App.v2.tsx                # v2 integrated root (all engines)
│       │
│       ├── types/
│       │   └── index.ts             # Shared TypeScript interfaces
│       │
│       ├── i18n/
│       │   └── index.ts             # Arabic/English translations
│       │
│       ├── services/                # Core service layer
│       │   ├── AppBootstrap.ts      # Unified engine lifecycle manager
│       │   ├── api.client.ts        # REST API client (axios-like)
│       │   ├── socket.manager.ts    # Socket.IO client singleton
│       │   ├── webrtc.manager.ts    # Legacy v1 WebRTC manager
│       │   │
│       │   ├── call/                # Call engine (v2)
│       │   │   ├── index.ts         # Barrel export
│       │   │   ├── CallStateMachine.ts    # 6-state FSM
│       │   │   ├── MediaDeviceManager.ts  # Device enumeration + switching
│       │   │   ├── PeerConnection.ts      # WebRTC peer wrapper
│       │   │   ├── GroupCallManager.ts    # Mesh topology (up to 8 peers)
│       │   │   ├── QualityController.ts   # Adaptive quality (5 presets)
│       │   │   ├── CallEngine.ts          # Top-level orchestrator
│       │   │   ├── ScreenShareManager.ts  # Screen capture + tracks
│       │   │   ├── PresenterManager.ts    # Presenter lock client
│       │   │   └── ScreenShareEngine.ts   # Screen share integration
│       │   │
│       │   └── messaging/           # Messaging engine (v2)
│       │       ├── index.ts         # Barrel export
│       │       ├── MessageQueue.ts        # Outbound queue + retry
│       │       ├── DeliveryTracker.ts     # Delivery/read state
│       │       ├── SyncManager.ts         # Reconnection sync
│       │       └── MessagingEngine.ts     # Top-level orchestrator
│       │
│       ├── stores/                  # Zustand state management
│       │   ├── auth.store.ts        # Auth + JWT + session restore
│       │   ├── contacts.store.ts    # Contacts + presence
│       │   ├── settings.store.ts    # App settings
│       │   ├── chat.store.ts        # v1 chat store
│       │   ├── chat.store.v2.ts     # v2 chat store (MessagingEngine)
│       │   ├── call.store.ts        # v1 call store
│       │   └── call.store.v2.ts     # v2 call store (CallEngine)
│       │
│       ├── hooks/
│       │   └── useAppListeners.ts   # Global cross-module event hook
│       │
│       ├── components/
│       │   ├── auth/
│       │   │   ├── LoginForm.tsx
│       │   │   └── RegisterForm.tsx
│       │   ├── layout/
│       │   │   ├── MainLayout.tsx
│       │   │   ├── Sidebar.tsx
│       │   │   └── TitleBar.tsx
│       │   ├── common/
│       │   │   ├── Avatar.tsx
│       │   │   ├── Modal.tsx
│       │   │   └── StatusBadge.tsx
│       │   ├── chat/
│       │   │   ├── ChatView.tsx
│       │   │   ├── ChannelList.tsx
│       │   │   ├── ChannelHeader.tsx
│       │   │   ├── MessageList.tsx
│       │   │   ├── MessageInput.tsx
│       │   │   ├── index.ts
│       │   │   └── dialogs/
│       │   │       ├── NewDMDialog.tsx
│       │   │       └── NewGroupDialog.tsx
│       │   ├── contacts/
│       │   │   └── ContactList.tsx
│       │   ├── groups/
│       │   │   └── GroupManager.tsx
│       │   ├── call/
│       │   │   ├── CallView.tsx
│       │   │   ├── CallControls.tsx
│       │   │   ├── IncomingCall.tsx
│       │   │   ├── ScreenSharePicker.tsx
│       │   │   └── ScreenShareOverlay.tsx
│       │   └── settings/
│       │       └── SettingsView.tsx
│       │
│       ├── pages/
│       │   └── CallHistoryPage.tsx
│       │
│       └── styles/
│           └── globals.css          # Tailwind base + custom styles
```

---

## Module Integration Map

### Authentication Flow
```
LoginForm → auth.store.login()
  → api.client.login() → POST /auth/login
  → socketManager.connect(url, token)
  → AppBootstrap.onLogin(userId)
    → CallEngine.init()
    → MessagingEngine.init()
    → ContactsStore.loadContacts()
    → ContactsStore.setupPresenceListeners()
```

### Messaging Flow (v2)
```
MessageInput → chatStore.sendMessage()
  → MessagingEngine.sendMessage()
    → MessageQueue.enqueue() → socket: v2_chat_send_message
    → DeliveryTracker.trackPending()
                            ↓
  Server: sync_handlers.v2_chat_send_message
    → message_service.create()
    → sync_service.create_receipts_for_message()
    → broadcast: v2_chat:new_message
    → mark online recipients delivered immediately
                            ↓
  MessagingEngine receives v2_chat:new_message
    → callback → chatStore.v2 updates messages[]
    → DeliveryTracker receives v2_chat:message_delivered
```

### Call Flow (v2)
```
ContactList "Call" → callStore.initiateCall()
  → CallEngine.initiateCall()
    → CallStateMachine: idle → ringing
    → MediaDeviceManager.acquireStream()
    → socket: v2_call_initiate
                            ↓
  Server: call_handlers.v2_call_initiate
    → call_service.create_call()
    → emit call_incoming to target user(s)
                            ↓
  Remote: CallEngine receives call_incoming
    → callStore.onIncomingCall → IncomingCall overlay
    → User clicks Accept → CallEngine.acceptCall()
      → socket: v2_call_accept
      → PeerConnection created → SDP offer/answer via call_signal
      → CallStateMachine: connecting → active
      → QualityController starts polling stats
```

### Screen Share Flow
```
CallView → callStore.startScreenShare(sourceId)
  → CallEngine.startScreenShare()
    → ScreenShareEngine.startSharing()
      → [Group?] PresenterManager.requestPresenter()
        → socket: presenter_request → server grants/queues
      → ScreenShareManager.start(source, mode, preset)
        → desktopCapturer.getSources() → getUserMedia()
        → PeerConnection.addScreenTrack() [dual-track]
          OR PeerConnection.replaceTrack() [replace-camera]
      → socket: v2_call_screen_share_start
                            ↓
  Remote: receives screen MediaStream via RTCPeerConnection
    → callStore.remoteStreams updated
    → ScreenShareOverlay renders full-screen video
```

### Reconnection Flow
```
Socket disconnect → SyncManager detects
  Socket reconnect → SyncManager.syncMissedMessages()
    → socket: sync_request { since: lastTimestamp }
    → Server returns missed messages + receipts
    → chatStore.v2 merges into messages[]
    → ContactsStore.loadContacts() (refresh presence)
```

---

## Socket Events Reference

### v2 Call Signaling (client → server)
| Event | Payload |
|---|---|
| v2_call_initiate | call_id, target_user_id, channel_id, media_type |
| v2_call_accept | call_id |
| v2_call_reject | call_id |
| v2_call_hangup | call_id |
| v2_call_join_group | call_id |
| v2_call_leave_group | call_id |
| v2_call_toggle_mute | call_id, muted |
| v2_call_toggle_video | call_id, video_off |
| v2_call_screen_share_start | call_id |
| v2_call_screen_share_stop | call_id |
| call_signal | call_id, target_user_id, signal |

### v2 Messaging (client → server)
| Event | Payload |
|---|---|
| v2_chat_send_message | channel_id, content, type, client_id, reply_to?, file_id? |
| v2_chat_mark_delivered | message_ids[] |
| v2_chat_mark_read | channel_id, message_ids[] |
| v2_chat_edit_message | message_id, content |
| v2_chat_delete_message | message_id |
| v2_chat_typing_start | channel_id |
| v2_chat_typing_stop | channel_id |
| v2_chat_reaction | message_id, emoji |
| sync_request | since (ISO timestamp) |

### Presenter Lock (client → server)
| Event | Payload |
|---|---|
| presenter_request | call_id |
| presenter_release | call_id |
| presenter_cancel_request | call_id |
| presenter_force_stop | call_id, target_user_id |
| presenter_get_state | call_id |

---

## Running the Project

### Prerequisites
- **Windows 10/11** (x64)
- **Python 3.10+** with pip
- **Node.js 18+** with npm

### Quick Start
```batch
REM Double-click or run:
start-commclient.bat

REM Or with PowerShell:
.\start-commclient.ps1
```

### Manual Start
```batch
REM Terminal 1 — Server
cd CommClient-Server
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python run.py

REM Terminal 2 — Desktop
cd CommClient-Desktop
npm install
npm run dev
```

### Production Build
```batch
cd CommClient-Desktop
scripts\build.bat
REM Output: CommClient-Desktop\release\CommClient Setup 1.0.0.exe
```

### Migration to v2
In `CommClient-Desktop/src/renderer/main.tsx`, change:
```typescript
// Before (v1):
import App from './App';

// After (v2 — all engines integrated):
import App from './App.v2';
```
