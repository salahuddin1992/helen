# Helen-Agent-Windows

Native Rust Windows device agent for the Helen / CommClient project. Registers
with the Helen-Server, maintains a heartbeat, executes whitelisted diagnostic
commands, transfers files inside a sandbox, and supports on-demand screen
capture and self-update.

## Build

```powershell
# Stable Rust toolchain (MSVC)
rustup default stable
rustup target add x86_64-pc-windows-msvc

cd C:\Users\youse\c\wifi\Helen-Agent-Windows
cargo build --release --target x86_64-pc-windows-msvc

# Produces:
# target\x86_64-pc-windows-msvc\release\helen-agent.exe (~3-5 MB)
```

## First-time pairing

```powershell
# Copy the binary to %ProgramFiles%\Helen-Agent\helen-agent.exe (or any path)
.\helen-agent.exe register --server https://helen-server.example.com
```

This generates the device fingerprint (stored in `HKLM\SOFTWARE\Helen-Agent`),
posts it to `/api/agents/register`, and persists `agent_id` + `refresh_token`
in `%ProgramData%\Helen-Agent\config.toml`.

## Install as a Windows Service

```powershell
# Must be elevated.
.\helen-agent.exe install
sc start HelenAgent
```

The service runs as `LocalSystem`, auto-starts on boot, and logs to
`%ProgramData%\Helen-Agent\logs\helen-agent.log.<date>` plus the Windows
Event Log.

## Sub-commands

| Command                  | Purpose                                         |
|--------------------------|-------------------------------------------------|
| `helen-agent install`    | Register as a Windows Service                   |
| `helen-agent uninstall`  | Remove the service                              |
| `helen-agent run`        | Foreground run (also used by the service host)  |
| `helen-agent register`   | First-time pairing with Helen-Server            |
| `helen-agent status`     | Print agent config + connectivity               |
| `helen-agent update`     | Check for a newer build and self-update         |
| `helen-agent fingerprint`| Print the stable device fingerprint             |

## Configuration

`%ProgramData%\Helen-Agent\config.toml`

```toml
server_url = "https://helen-server.example.com"
heartbeat_interval_secs = 30
command_timeout_secs = 30
log_level = "info"
verify_tls = true
http_timeout_secs = 30
screen_capture_requires_consent = true
command_whitelist = [
  "ipconfig", "tasklist", "systeminfo", "netstat",
  "whoami", "hostname", "ping", "tracert", "nslookup", "getmac"
]
```

## Operational footprint

* Binary size: 3 – 5 MB (LTO + strip + opt-level "z")
* Resident memory: ~10 MB idle, ~25 MB during snapshot collection
* CPU: <1 % idle, brief spike per heartbeat
* Network: ~5–15 KB per heartbeat (system snapshot JSON)

## Update flow

1. Agent polls `GET /api/agents/update/manifest`.
2. Server returns `{ version, url, sha256, signature? }`.
3. Agent downloads to `%TEMP%\Helen-Agent-Update\helen-agent-update.exe`.
4. SHA-256 verified.
5. Helper batch script swaps the running executable and restarts the service.
