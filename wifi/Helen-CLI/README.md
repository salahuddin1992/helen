# helen-cli

Headless command-line client for the Helen / CommClient platform.

## Install

```powershell
cd C:\Users\youse\c\wifi\Helen-CLI
cargo build --release --target x86_64-pc-windows-msvc
# Resulting binary:
# .\target\x86_64-pc-windows-msvc\release\helen-cli.exe
```

(Linux/macOS dev builds work too — just `cargo build --release`.)

## First-run

```bash
helen-cli login --server https://helen.lan --username alice
helen-cli whoami
helen-cli channels
```

Refresh-token is stored in the OS keyring (`helen-cli` / `refresh_token`).
Other config lives in `~/.helen/config.toml`.

## Common usage

```bash
helen-cli messages --channel general --limit 50
helen-cli messages --channel general --watch          # live tail
helen-cli send --channel general "hello team"
helen-cli edit <message_id> "fixed typo"
helen-cli delete <message_id>

helen-cli upload --channel general .\report.pdf
helen-cli download <file_id> --out .\report.pdf --resume

helen-cli pair                                        # request 6-digit code + QR
helen-cli pair --code 123456                          # redeem code from another device

helen-cli call --channel general --audio --seconds 600
helen-cli agent                                       # interactive REPL with tab-complete
```

## Configuration keys

| key              | meaning                                              |
|------------------|------------------------------------------------------|
| `server_url`     | base Helen server URL (e.g. `https://helen.lan`)     |
| `default_channel`| pre-fill `--channel` if omitted                       |
| `audio_input`    | cpal input device name (omit = default)              |
| `audio_output`   | cpal output device name                              |

Set via:

```bash
helen-cli config --set server_url=https://helen.lan
helen-cli config
```

## Audio call notes

The call implementation uses a server-relayed degraded path: PCM is
captured via cpal, encoded with Opus 48 kHz mono in 20 ms frames, and
pushed through the existing Socket.IO `audio:frame` event channel. This
keeps the CLI usable behind strict NATs / proxies where direct media
paths fail (matches the mobile/desktop fallback).

## Logging

```bash
helen-cli -v channels        # debug
helen-cli -vv channels       # trace
RUST_LOG=helen_cli=trace,reqwest=info helen-cli channels
```
