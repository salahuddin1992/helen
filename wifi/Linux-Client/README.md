# Helen Client · Linux

Linux packaging for the Electron desktop client (`CommClient-Desktop`).
Three distribution formats supported:

| Format | Install | Best for |
|---|---|---|
| **AppImage** | single file, runs anywhere | grab-and-go, no root |
| **deb** | `apt install ./Helen_*.deb` | Debian / Ubuntu / Mint |
| **rpm** | `rpm -i Helen-*.rpm` | Fedora / RHEL / openSUSE |
| **tar.gz** | extract + run | CI, custom installs |

## Install from pre-built artifact

```bash
# AppImage
chmod +x Helen-1.0.0.AppImage
./Helen-1.0.0.AppImage

# deb
sudo apt install ./Helen_1.0.0_amd64.deb

# rpm
sudo dnf install ./Helen-1.0.0.x86_64.rpm
```

Or use our installer:
```bash
sudo ./install.sh            # detects distro + picks best format
sudo ./install.sh --appimage # force AppImage into /opt/helen/client
```

## Build from source

```bash
./scripts/build-appimage.sh          # → ../CommClient-Desktop/release/*.AppImage
./scripts/build-deb.sh               # → ../CommClient-Desktop/release/*.deb
./scripts/build-rpm.sh               # → ../CommClient-Desktop/release/*.rpm
./scripts/build-all.sh               # all three
```

Build dependencies:
- Node.js 18+
- npm 9+ (or pnpm)
- For AppImage: `fuse` (Ubuntu: `sudo apt install libfuse2`)
- For deb:  `dpkg-deb`, `fakeroot`
- For rpm:  `rpmbuild`

## Post-install

Launch from app menu ("Helen") or CLI:
```bash
helen                # standard launch
helen --devtools     # open devtools at boot
helen --server http://192.168.1.10:3000   # override auto-discovery
```

The client auto-discovers Helen-Server on the LAN via UDP broadcast on
port 41234. To pin a specific server, use Advanced Settings in the
client UI or pass `--server` at launch.

## Uninstall

```bash
sudo ./uninstall.sh              # removes binaries, leaves user data
sudo ./uninstall.sh --purge      # also wipes ~/.config/Helen and DB
```

## User data locations

Electron follows XDG:
- Config: `~/.config/Helen/`
- Cache:  `~/.cache/Helen/`
- Logs:   `~/.config/Helen/logs/`

Clearing these resets the client to first-run state.

## Known Linux-specific issues

- **Wayland**: works on Wayland, but screen-share uses PipeWire; confirm
  `xdg-desktop-portal` is installed (`apt install xdg-desktop-portal-gtk`
  or `xdg-desktop-portal-kde`)
- **Audio**: defaults to PulseAudio; for PipeWire-only systems, ensure
  `pipewire-pulse` provides the Pulse compatibility layer
- **Camera permission**: desktop portals gate camera — accept the
  permission popup at first call
- **NetworkManager sleep**: if you lose LAN discovery after suspend,
  restart the client (Electron's networkService caches broadcast socket
  state; the UI refresh button re-binds)
