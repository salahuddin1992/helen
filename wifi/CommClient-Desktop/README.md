# CommClient Desktop — Windows LAN Communication Client

Electron + React + TypeScript desktop application for CommClient, a LAN-only real-time communication platform.

## Quick Start (Windows)

### Prerequisites

- Node.js 18+ (LTS)
- npm 9+
- CommClient-Server running on the LAN

### Setup

```powershell
cd CommClient-Desktop

# Install dependencies
npm install

# Start in development mode (with hot-reload)
npm run dev
```

The app opens automatically. Point it to your CommClient server address (e.g., `http://192.168.1.100:3000`).

### Build for Production

```powershell
# Build and package as Windows installer
npm run build

# Output: release/CommClient-Setup-1.0.0.exe
```

---

## Architecture

```
CommClient-Desktop/
├── src/
│   ├── main/                       # Electron Main Process
│   │   └── index.ts                # Window, tray, IPC, global shortcuts
│   ├── preload/
│   │   └── index.ts                # Secure contextBridge API
│   └── renderer/                   # React UI (39 source files, 6500+ lines)
│       ├── App.tsx                  # Root: routing, auth guard, overlays
│       ├── main.tsx                 # Entry point
│       ├── components/
│       │   ├── auth/               # LoginForm, RegisterForm
│       │   ├── layout/             # TitleBar, Sidebar, MainLayout
│       │   ├── common/             # Avatar, Modal, StatusBadge
│       │   ├── chat/               # ChatView, ChannelList, MessageList, MessageInput, ChannelHeader, Dialogs
│       │   ├── call/               # CallView, CallControls, IncomingCall, ScreenSharePicker
│       │   ├── contacts/           # ContactList
│       │   ├── groups/             # GroupManager
│       │   └── settings/           # SettingsView
│       ├── services/
│       │   ├── api.client.ts       # REST API client with auto-refresh
│       │   ├── socket.manager.ts   # Socket.IO connection + event bus
│       │   └── webrtc.manager.ts   # P2P calls via simple-peer
│       ├── stores/                 # Zustand state management
│       │   ├── auth.store.ts       # Login, session restore, tokens
│       │   ├── chat.store.ts       # Channels, messages, typing
│       │   ├── call.store.ts       # Call lifecycle, WebRTC, screen share
│       │   ├── contacts.store.ts   # Contacts, presence
│       │   └── settings.store.ts   # App preferences
│       ├── i18n/index.ts           # English + Arabic translations
│       ├── types/index.ts          # Full TypeScript definitions
│       ├── pages/                  # Page components
│       └── styles/globals.css      # Tailwind + custom styles
├── index.html
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── tsconfig.json
└── electron-builder config (in package.json)
```

## Features

### Communication
- Private 1-to-1 text messaging
- Group text messaging with channels
- 1-to-1 audio/video calls (P2P via WebRTC)
- Group audio/video calls (SFU signaling ready)
- Screen sharing (Electron desktopCapturer)

### UI/UX
- Dark theme with Tailwind CSS
- Custom frameless window with title bar controls
- System tray with background running
- Global keyboard shortcuts (Ctrl+Shift+M = mute, etc.)
- Real-time typing indicators
- Message reactions
- Unread message badges
- Online/offline/busy/away presence indicators
- Arabic and English (RTL/LTR) support

### Desktop Integration
- Windows NSIS installer
- System tray icon
- Native notifications
- Auto-launch option
- Picture-in-picture call window

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+Shift+M | Toggle mute |
| Ctrl+Shift+V | Toggle camera |
| Ctrl+Shift+E | End call |
| Enter | Send message |
| Shift+Enter | New line in message |

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Shell | Electron 30 |
| UI | React 18 + TypeScript |
| State | Zustand |
| Styling | Tailwind CSS |
| Real-time | Socket.IO Client |
| WebRTC | simple-peer |
| Routing | React Router v6 |
| Bundler | Vite |
| Package | electron-builder (NSIS) |
