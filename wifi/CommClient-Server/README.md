# CommClient Server — LAN Communication Backend

Production-grade Python backend for CommClient, a LAN-only real-time communication platform.

## Quick Start (Windows)

### Prerequisites

- Python 3.10+ installed and on PATH
- Windows 10/11

### Setup

```powershell
# Clone or copy the project
cd CommClient-Server

# Create virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Create data directory
mkdir data
mkdir data\files
```

### Run

```powershell
# Activate venv if not active
.\venv\Scripts\Activate.ps1

# Start the server
python run.py
```

Server will start on `http://0.0.0.0:3000` and auto-discover LAN clients via UDP broadcast.

### Environment Configuration

Copy `.env` and adjust as needed. Key settings:

```env
HOST=0.0.0.0
PORT=3000
DEBUG=false
SQLITE_PATH=./data/commclient.db
JWT_SECRET=your-secret-key-here
```

### Windows Firewall

Open these ports for LAN communication:

```powershell
# Run as Administrator
netsh advfirewall firewall add rule name="CommClient HTTP" dir=in action=allow protocol=tcp localport=3000
netsh advfirewall firewall add rule name="CommClient Discovery" dir=in action=allow protocol=udp localport=41234
netsh advfirewall firewall add rule name="CommClient Media" dir=in action=allow protocol=udp localport=40000-49999
```

### Run as Windows Service (Production)

```powershell
# Install NSSM (Non-Sucking Service Manager)
choco install nssm
# Or download from https://nssm.cc

# Create service
nssm install CommClient "C:\Path\To\venv\Scripts\python.exe" "C:\Path\To\run.py"
nssm set CommClient AppDirectory "C:\Path\To\CommClient-Server"
nssm set CommClient Start SERVICE_AUTO_START
nssm start CommClient
```

---

## Architecture

```
CommClient-Server/
├── app/
│   ├── api/routes/       # REST endpoints (36 routes)
│   ├── core/             # Config, security, deps, exceptions, logging
│   ├── db/               # SQLAlchemy engine, base models
│   ├── models/           # 9 ORM models (User, Channel, Message, etc.)
│   ├── schemas/          # Pydantic request/response schemas
│   ├── services/         # Business logic (auth, chat, calls, presence, discovery)
│   ├── socket/           # Socket.IO event handlers (25+ events)
│   └── utils/            # Network utilities
├── data/                 # Runtime data (SQLite DB, uploaded files)
├── run.py                # Server launcher
├── requirements.txt      # Python dependencies
└── .env                  # Configuration
```

## REST API (36 endpoints)

### Auth
| Method | Path | Description |
|--------|------|-------------|
| POST | /api/auth/register | Create user account |
| POST | /api/auth/login | Login + receive JWT |
| POST | /api/auth/refresh | Refresh tokens |
| POST | /api/auth/logout | Revoke session |

### Users & Contacts
| Method | Path | Description |
|--------|------|-------------|
| GET | /api/users | List all users |
| GET | /api/users/me | Current user profile |
| PATCH | /api/users/me | Update profile |
| GET | /api/users/{id} | Get user by ID |
| GET | /api/users/me/contacts | List contacts |
| POST | /api/users/me/contacts | Add contact |
| PATCH | /api/users/me/contacts/{id} | Update contact |
| DELETE | /api/users/me/contacts/{id} | Remove contact |

### Channels / Rooms
| Method | Path | Description |
|--------|------|-------------|
| POST | /api/channels | Create DM or group |
| GET | /api/channels | List user's channels |
| GET | /api/channels/{id} | Channel details |
| PATCH | /api/channels/{id} | Update channel |
| POST | /api/channels/{id}/members | Add member |
| DELETE | /api/channels/{id}/members/{uid} | Remove member |

### Messages
| Method | Path | Description |
|--------|------|-------------|
| GET | /api/channels/{id}/messages | Get message history |
| POST | /api/channels/{id}/messages | Send message |
| GET | /api/messages/search | Search messages |
| PATCH | /api/messages/{id} | Edit message |
| DELETE | /api/messages/{id} | Delete message |
| POST | /api/messages/{id}/reactions | Toggle reaction |
| POST | /api/messages/{id}/read | Mark as read |

### Files
| Method | Path | Description |
|--------|------|-------------|
| POST | /api/files/upload | Upload file |
| GET | /api/files/{id} | Download file |
| GET | /api/files/{id}/thumbnail | Get thumbnail |
| DELETE | /api/files/{id} | Delete file |

### Sessions / Devices
| Method | Path | Description |
|--------|------|-------------|
| GET | /api/sessions | List active sessions |
| DELETE | /api/sessions/{id} | Revoke session |
| POST | /api/sessions/revoke-all | Revoke all sessions |

### Calls & System
| Method | Path | Description |
|--------|------|-------------|
| GET | /api/calls | Call history |
| GET | /api/health | Health check |
| GET | /api/info | Server info |

## Socket.IO Events (25+ events)

### Presence Events
| Direction | Event | Description |
|-----------|-------|-------------|
| Client → Server | `presence_heartbeat` | Keep-alive |
| Client → Server | `presence_set_status` | Set status (online/away/busy/dnd) |
| Server → Client | `presence:user_online` | User came online |
| Server → Client | `presence:user_offline` | User went offline |
| Server → Client | `presence:user_status` | Status changed |
| Server → Client | `presence:online_list` | Full online user list |

### Chat Events
| Direction | Event | Description |
|-----------|-------|-------------|
| Client → Server | `chat_send_message` | Send a message |
| Client → Server | `chat_typing_start` | Started typing |
| Client → Server | `chat_typing_stop` | Stopped typing |
| Client → Server | `chat_message_read` | Mark messages read |
| Client → Server | `chat_reaction` | Toggle reaction |
| Server → Client | `chat:new_message` | New message received |
| Server → Client | `chat:typing` | Typing indicator |
| Server → Client | `chat:delivery_receipt` | Delivery/read receipt |
| Server → Client | `chat:reaction_update` | Reaction changed |

### Call Events
| Direction | Event | Description |
|-----------|-------|-------------|
| Client → Server | `call_initiate` | Start 1-to-1 call |
| Client → Server | `call_accept` | Accept call |
| Client → Server | `call_reject` | Reject call |
| Client → Server | `call_hangup` | End call |
| Client → Server | `call_join_group` | Join group call |
| Client → Server | `call_leave_group` | Leave group call |
| Server → Client | `call:incoming` | Incoming call |
| Server → Client | `call:accepted` | Call accepted |
| Server → Client | `call:rejected` | Call rejected |
| Server → Client | `call:ended` | Call ended |
| Server → Client | `call:peer_ready` | Ready for signaling |
| Server → Client | `call:peer_joined` | New peer in group call |
| Server → Client | `call:peer_left` | Peer left group call |
| Server → Client | `call:group_ringing` | Group call ring |

### WebRTC Signaling
| Direction | Event | Description |
|-----------|-------|-------------|
| Client → Server | `signal_offer` | SDP offer relay |
| Client → Server | `signal_answer` | SDP answer relay |
| Client → Server | `signal_ice_candidate` | ICE candidate relay |
| Server → Client | `signal:offer` | Forwarded SDP offer |
| Server → Client | `signal:answer` | Forwarded SDP answer |
| Server → Client | `signal:ice_candidate` | Forwarded ICE candidate |

### Media Control Events
| Direction | Event | Description |
|-----------|-------|-------------|
| Client → Server | `call_toggle_mute` | Toggle audio mute |
| Client → Server | `call_toggle_video` | Toggle video |
| Client → Server | `call_screen_share_start` | Start screen share |
| Client → Server | `call_screen_share_stop` | Stop screen share |
| Server → Client | `call:participant_muted` | Participant muted |
| Server → Client | `call:participant_video` | Participant video toggle |
| Server → Client | `call:screen_share_started` | Screen share started |
| Server → Client | `call:screen_share_stopped` | Screen share stopped |

## Database Models

9 tables with full relational schema:
- **users** — Accounts, profiles, presence
- **user_sessions** — JWT session tracking per device
- **contacts** — Bidirectional buddy lists
- **channels** — DM and group channels
- **channel_members** — Channel membership with roles
- **messages** — All message types with soft delete
- **reactions** — Message reactions (emoji)
- **files** — Uploaded file metadata
- **call_logs** — Call history with duration

## Migration to PostgreSQL

1. Install asyncpg: `pip install asyncpg`
2. Update `.env`:
   ```env
   DB_BACKEND=postgresql
   DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/commclient
   ```
3. Restart the server — tables are auto-created

For production migrations, use Alembic:
```bash
alembic init migrations
alembic revision --autogenerate -m "initial"
alembic upgrade head
```
