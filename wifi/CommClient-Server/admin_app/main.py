"""
Helen SERVER — Admin Desktop Application.

Single-exe native desktop program (Edge WebView2) that:
  1. Spawns and manages the Helen-Server.exe backend as a child process.
  2. Serves the admin dashboard (admin/index.html) over 127.0.0.1:5173 so the
     browser origin satisfies the server's CORS allowlist.
  3. Exposes a JS bridge (`window.pywebview.api.*`) for the dashboard to start,
     stop, restart, and probe the backend lifecycle without shelling out.
  4. Runs a background health monitor that pushes status events into the page.
  5. Lives in the system tray — closing the window hides to tray; the server
     keeps running until the user picks Quit from the tray menu.
  6. Prevents duplicate instances via a named Windows mutex.
  7. Offers a "Start with Windows" toggle that writes to HKCU\\...\\Run.
  8. Shuts the backend down cleanly when the tray is told to Quit.

Design:
  - Independent of the Electron client project. This is the server operator's
    tool, shipped as its own PyInstaller --onedir bundle next to the server.
  - When frozen, looks for `server/Helen-Server.exe` beside the admin exe. When
    running from source, falls back to `../dist/Helen-Server/Helen-Server.exe`.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

# ── Configuration ──────────────────────────────────────────

STATIC_PORT = 5173
SERVER_PORT = 3000
ADMIN_FOLDER = "admin"
SERVER_FOLDER_NAME = "Helen-Server"
SERVER_EXE_NAME = "Helen-Server.exe"
ADMIN_EXE_NAME = "Helen-Admin.exe"
WINDOW_TITLE = "Helen — Admin Console"
TRAY_TITLE = "Helen Admin"
BG_COLOR = "#0b1220"

# Mutex + registry identifiers. Global\ makes the mutex visible across user
# sessions — prevents two operators on the same box from double-spawning.
SINGLE_INSTANCE_MUTEX = "Global\\HelenAdmin.SingleInstance.v1"
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "HelenAdmin"

START_READY_TIMEOUT_SEC = 30.0
STOP_GRACE_TIMEOUT_SEC = 10.0
HEALTH_POLL_INTERVAL_SEC = 3.0

# LAN discovery — listen for Helen-Server UDP broadcasts and verify via HTTP.
DISCOVERY_UDP_PORT = 41234
DISCOVERY_STALE_SEC = 15.0
DISCOVERY_VERIFY_TIMEOUT_SEC = 2.5


# ── Resource resolution ───────────────────────────────────


def _frozen_base() -> Path:
    """Resolve the base directory for bundled resources.

    - PyInstaller onedir/onefile: sys._MEIPASS points to the extracted payload.
    - Running from source: use this file's directory.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(os.path.dirname(os.path.abspath(__file__)))


def _exe_dir() -> Path:
    """Directory containing the running exe (or this script in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    """Locate a bundled resource across PyInstaller one-file/onedir layouts."""
    base = _frozen_base()
    candidates = [
        base / rel,
        base.parent / rel,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0])


def server_exe_path() -> str | None:
    """Find Helen-Server.exe — bundled beside admin exe or in the server dist."""
    base = _frozen_base()
    candidates = [
        base / "server" / SERVER_EXE_NAME,
        base.parent / "server" / SERVER_EXE_NAME,
        # Dev layout (admin_app/main.py → ../dist/Helen-Server/Helen-Server.exe)
        base.parent / "dist" / SERVER_FOLDER_NAME / SERVER_EXE_NAME,
        base / "dist" / SERVER_FOLDER_NAME / SERVER_EXE_NAME,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


# ── Single-instance guard (Windows named mutex) ───────────


class SingleInstanceGuard:
    """Hold a named Windows mutex for the lifetime of the process.

    If another Helen-Admin is already running, `acquired` is False and the
    caller should exit instead of spawning a duplicate server child.
    """

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        self.acquired = False
        self._handle = None
        if sys.platform != "win32":
            # Non-Windows dev — no-op, act as acquired.
            self.acquired = True
            return
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateMutexW.restype = ctypes.c_void_p
            k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            self._handle = k32.CreateMutexW(None, True, name)
            last_err = ctypes.get_last_error() or ctypes.GetLastError()
            self.acquired = last_err != self.ERROR_ALREADY_EXISTS
        except Exception:
            # If the OS refuses the mutex we degrade to best-effort start.
            self.acquired = True

    def release(self) -> None:
        if self._handle and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


# ── Autostart (HKCU Run key) ──────────────────────────────


class AutostartManager:
    """Toggle Helen-Admin auto-start at user login via HKCU Run.

    HKCU doesn't need admin rights; entries run at interactive logon. When
    enabled, we launch Helen-Admin with --hidden so it goes straight to the
    tray without flashing a window on every boot.
    """

    def __init__(self) -> None:
        self._available = sys.platform == "win32"

    def _exe_command(self) -> str:
        exe = str(_exe_dir() / ADMIN_EXE_NAME) if getattr(sys, "frozen", False) else sys.executable
        return f'"{exe}" --hidden'

    def is_enabled(self) -> bool:
        if not self._available:
            return False
        try:
            import winreg  # local import — Windows-only module

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as k:
                val, _ = winreg.QueryValueEx(k, AUTOSTART_REG_NAME)
                return bool(val)
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def enable(self) -> bool:
        if not self._available:
            return False
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE
            ) as k:
                winreg.SetValueEx(k, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, self._exe_command())
            return True
        except OSError:
            return False

    def disable(self) -> bool:
        if not self._available:
            return False
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE
            ) as k:
                winreg.DeleteValue(k, AUTOSTART_REG_NAME)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def toggle(self) -> bool:
        return self.disable() if self.is_enabled() else self.enable()


# ── Port/health probes ────────────────────────────────────


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _fetch_health() -> dict | None:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{SERVER_PORT}/api/health")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)
    except Exception:
        return None


# ── Static HTTP for admin HTML ────────────────────────────


def _start_static_server(
    directory: str, port: int, *, expose_on_lan: bool = False,
) -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        # Silence default stdout access log; we run in a windowed exe.
        def log_message(self, fmt, *args):  # noqa: A003, ARG002
            pass

    # `expose_on_lan` flips the bind host from loopback to all interfaces
    # so any browser on the LAN can open http://<operator-host-ip>:5173/.
    # Default stays loopback-only — the pywebview shell is the intended UI.
    bind_host = "0.0.0.0" if expose_on_lan else "127.0.0.1"
    with socketserver.TCPServer((bind_host, port), Handler) as httpd:
        httpd.serve_forever()


# ── Backend lifecycle controller ──────────────────────────


class ServerController:
    """Spawn and supervise the Helen-Server.exe subprocess."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        atexit.register(self.stop)

    # Public API ---------------------------------------------

    def status(self) -> dict:
        with self._lock:
            managed = self._process is not None and self._process.poll() is None
            pid = self._process.pid if managed else None
        return {
            "listening": _port_listening(SERVER_PORT),
            "managed": managed,
            "pid": pid,
            "port": SERVER_PORT,
            "exe": server_exe_path(),
            "exe_found": server_exe_path() is not None,
        }

    def start(self) -> dict:
        with self._lock:
            if _port_listening(SERVER_PORT):
                return {
                    "ok": True,
                    "managed": self._process is not None,
                    "message": "Server already listening",
                }
            exe = server_exe_path()
            if not exe:
                return {"ok": False, "error": "Helen-Server.exe not found"}
            try:
                flags = 0
                if sys.platform == "win32":
                    # Hide the server's console window (we want pure desktop UX).
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                # Child env: redirect logs to <admin_exe_dir>/logs so an
                # operator who clicks "Open logs folder" lands in a sane
                # place — not buried under _internal/server/_internal/logs.
                child_env = dict(os.environ)
                log_dir = _exe_dir() / "logs"
                try:
                    log_dir.mkdir(parents=True, exist_ok=True)
                    child_env["LOG_DIR"] = str(log_dir)
                except OSError:
                    pass
                self._process = subprocess.Popen(
                    [exe],
                    cwd=str(Path(exe).parent),
                    creationflags=flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=child_env,
                )
            except Exception as e:
                self._process = None
                return {"ok": False, "error": f"spawn failed: {e}"}

        # Wait outside the lock so status() / health pollers can observe state.
        deadline = time.monotonic() + START_READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if _port_listening(SERVER_PORT):
                return {"ok": True, "pid": self._process.pid if self._process else None}
            if self._process and self._process.poll() is not None:
                return {
                    "ok": False,
                    "error": f"process exited with code {self._process.returncode}",
                }
            time.sleep(0.3)
        return {"ok": False, "error": "did not become ready within timeout"}

    def stop(self) -> dict:
        with self._lock:
            proc = self._process
            self._process = None
        if proc is None:
            return {"ok": True, "message": "no managed process"}
        if proc.poll() is not None:
            return {"ok": True, "message": "already exited"}
        try:
            if sys.platform == "win32":
                # taskkill /T tears down child processes too.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=STOP_GRACE_TIMEOUT_SEC,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_GRACE_TIMEOUT_SEC)
                except subprocess.TimeoutExpired:
                    proc.kill()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def restart(self) -> dict:
        self.stop()
        # Brief cooldown so the OS releases the listen socket (Windows lingers).
        time.sleep(1.5)
        return self.start()


# ── LAN discovery — listen for Helen-Server UDP broadcasts ─


class LanDiscovery:
    """Passive UDP listener on 41234 that tracks Helen-Server instances on
    the local network. Matches the broadcast contract in
    ``app.services.discovery_service`` on the server side.

    State is thread-safe (ops hold ``_lock``). The dashboard polls via
    :meth:`snapshot`; a quick HTTP verify per unique server is done in a
    background worker to populate ``verified``/``rtt_ms``.
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._servers: dict[str, dict] = {}
        self._listener: threading.Thread | None = None
        self._verifier: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._listener and self._listener.is_alive():
            return
        self._stop.clear()
        self._listener = threading.Thread(
            target=self._listen_loop, name="helen-admin-discovery", daemon=True,
        )
        self._listener.start()
        self._verifier = threading.Thread(
            target=self._verify_loop, name="helen-admin-discovery-verify", daemon=True,
        )
        self._verifier.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None

    def _open_socket(self) -> socket.socket | None:
        """Open the UDP listener with reuse + broadcast flags. Retries on error."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind(("0.0.0.0", DISCOVERY_UDP_PORT))
            s.settimeout(1.0)
            return s
        except OSError:
            return None

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            if self._sock is None:
                self._sock = self._open_socket()
                if self._sock is None:
                    time.sleep(2.0)
                    continue
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                self._expire_stale()
                continue
            except OSError:
                self._sock = None
                time.sleep(1.0)
                continue
            try:
                packet = json.loads(data.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if packet.get("type") != "commclient-server":
                continue
            sid = packet.get("server_id") or f"{addr[0]}:{packet.get('port', SERVER_PORT)}"
            host = packet.get("host") or addr[0]
            port = int(packet.get("port") or SERVER_PORT)
            with self._lock:
                prev = self._servers.get(sid, {})
                self._servers[sid] = {
                    "server_id": sid,
                    "name": packet.get("name") or "Helen Server",
                    "host": host,
                    "port": port,
                    "url": f"http://{host}:{port}",
                    "version": packet.get("version") or prev.get("version") or "?",
                    "users_online": packet.get("users_online") or 0,
                    "uptime": packet.get("uptime") or 0,
                    "verified": prev.get("verified", False),
                    "rtt_ms": prev.get("rtt_ms"),
                    "last_seen": time.time(),
                    "discovery_method": "udp",
                }

    def _verify_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(2.0)
            # Verify each known server once every ~8s (rate-limited per entry).
            targets = []
            now = time.time()
            with self._lock:
                for sid, entry in self._servers.items():
                    last_check = entry.get("_last_check", 0)
                    if now - last_check > 8:
                        entry["_last_check"] = now
                        targets.append((sid, entry["url"]))
            for sid, url in targets:
                t0 = time.monotonic()
                ok = False
                try:
                    with urllib.request.urlopen(
                        url + "/api/discovery",
                        timeout=DISCOVERY_VERIFY_TIMEOUT_SEC,
                    ) as resp:
                        if resp.status == 200:
                            ok = True
                except Exception:
                    ok = False
                rtt_ms = int((time.monotonic() - t0) * 1000)
                with self._lock:
                    entry = self._servers.get(sid)
                    if entry is not None:
                        entry["verified"] = ok
                        entry["rtt_ms"] = rtt_ms if ok else None

    def _expire_stale(self) -> None:
        cutoff = time.time() - DISCOVERY_STALE_SEC
        with self._lock:
            dead = [sid for sid, e in self._servers.items() if e["last_seen"] < cutoff]
            for sid in dead:
                self._servers.pop(sid, None)

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            for entry in self._servers.values():
                clone = {k: v for k, v in entry.items() if not k.startswith("_")}
                out.append(clone)
        # Rank: verified first, then lowest RTT, then most users.
        out.sort(key=lambda s: (
            0 if s.get("verified") else 1,
            s.get("rtt_ms") if s.get("rtt_ms") is not None else 99999,
            -int(s.get("users_online") or 0),
        ))
        return out

    def add_manual(self, url: str) -> dict | None:
        """Probe a manually-entered server URL and add it to the known set."""
        url = (url or "").strip().rstrip("/")
        if not url.startswith("http"):
            url = "http://" + url
        try:
            t0 = time.monotonic()
            with urllib.request.urlopen(
                url + "/api/discovery", timeout=DISCOVERY_VERIFY_TIMEOUT_SEC,
            ) as resp:
                if resp.status != 200:
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        rtt_ms = int((time.monotonic() - t0) * 1000)
        sid = payload.get("server_id") or url
        entry = {
            "server_id": sid,
            "name": payload.get("name") or "Helen Server",
            "host": payload.get("host") or url,
            "port": int(payload.get("port") or SERVER_PORT),
            "url": url,
            "version": payload.get("version") or "?",
            "users_online": payload.get("users_online") or 0,
            "uptime": payload.get("uptime") or 0,
            "verified": True,
            "rtt_ms": rtt_ms,
            "last_seen": time.time(),
            "discovery_method": "manual",
        }
        with self._lock:
            self._servers[sid] = entry
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    # ── Active LAN scan (forced fallback) ─────────────────
    #
    # When UDP broadcast is blocked (corporate WiFi, guest networks, strict
    # firewalls) passive discovery returns nothing. We must still connect, so
    # this method enumerates local /24 subnets and fires HEAD probes against
    # the canonical server port(s) on every host concurrently. Hits are added
    # to the snapshot as if they'd been discovered over UDP.
    #
    # Cost: ~254 probes × |subnets| × |ports|, each ~300ms timeout. With a
    # 64-thread pool a single /24 completes in ~1.5s.

    _SCAN_PORTS: tuple[int, ...] = (SERVER_PORT, 3001)
    _SCAN_CONCURRENCY = 64
    _SCAN_PROBE_TIMEOUT = 0.30

    def _local_subnets(self) -> list[str]:
        """Return list of /24 network prefixes (e.g. '192.168.1.') for every
        non-loopback, non-APIPA IPv4 interface.
        """
        prefixes: list[str] = []
        seen: set[str] = set()
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                parts = ip.split(".")
                if len(parts) != 4:
                    continue
                prefix = ".".join(parts[:3]) + "."
                if prefix not in seen:
                    seen.add(prefix)
                    prefixes.append(prefix)
        except Exception:
            pass
        return prefixes

    def _probe_tcp(self, host: str, port: int) -> bool:
        """Cheap liveness check — open a TCP connection, discard. Avoids the
        HTTP handshake cost for ~250 dead hosts on every scan."""
        try:
            with socket.create_connection((host, port), timeout=self._SCAN_PROBE_TIMEOUT):
                return True
        except OSError:
            return False

    def _identify_helen(self, host: str, port: int) -> dict | None:
        """For a host that answered TCP, verify it's actually a Helen server
        by hitting /api/discovery. Returns the server's payload or None."""
        url = f"http://{host}:{port}"
        try:
            with urllib.request.urlopen(
                url + "/api/discovery", timeout=DISCOVERY_VERIFY_TIMEOUT_SEC,
            ) as resp:
                if resp.status != 200:
                    return None
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        if payload.get("type") != "commclient-server":
            return None
        payload["__url"] = url
        return payload

    def active_scan(self, subnets: list[str] | None = None) -> dict:
        """Force-find Helen servers by probing every host on every local /24.

        Returns a summary: {scanned, found, subnets}. Adds any Helen servers
        found to the internal table so :meth:`snapshot` exposes them to the UI.
        """
        import concurrent.futures as cf

        if not subnets:
            subnets = self._local_subnets()
        if not subnets:
            return {"scanned": 0, "found": 0, "subnets": []}

        targets: list[tuple[str, int]] = []
        for prefix in subnets:
            for host_octet in range(1, 255):
                for port in self._SCAN_PORTS:
                    targets.append((f"{prefix}{host_octet}", port))

        live_hosts: list[tuple[str, int]] = []
        with cf.ThreadPoolExecutor(max_workers=self._SCAN_CONCURRENCY) as pool:
            fut_to_addr = {
                pool.submit(self._probe_tcp, h, p): (h, p) for h, p in targets
            }
            for fut in cf.as_completed(fut_to_addr):
                if fut.result():
                    live_hosts.append(fut_to_addr[fut])

        # Verify each live host is actually Helen.
        found = 0
        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            fut_to_addr = {
                pool.submit(self._identify_helen, h, p): (h, p) for h, p in live_hosts
            }
            for fut in cf.as_completed(fut_to_addr):
                host, port = fut_to_addr[fut]
                payload = fut.result()
                if not payload:
                    continue
                sid = payload.get("server_id") or f"{host}:{port}"
                with self._lock:
                    prev = self._servers.get(sid, {})
                    self._servers[sid] = {
                        "server_id": sid,
                        "name": payload.get("name") or "Helen Server",
                        "host": payload.get("host") or host,
                        "port": int(payload.get("port") or port),
                        "url": payload["__url"],
                        "version": payload.get("version") or "?",
                        "users_online": payload.get("users_online") or 0,
                        "uptime": payload.get("uptime") or 0,
                        "verified": True,
                        "rtt_ms": prev.get("rtt_ms"),
                        "last_seen": time.time(),
                        "discovery_method": "active_scan",
                    }
                found += 1

        return {
            "scanned": len(targets),
            "found": found,
            "subnets": list(subnets),
            "live_tcp_hits": len(live_hosts),
        }

    def auto_escalate_if_silent(self, wait_sec: float = 5.0) -> None:
        """Background helper — if no UDP broadcasts arrive within `wait_sec`
        after startup, fire an active scan automatically.
        """
        def runner():
            time.sleep(wait_sec)
            if self._stop.is_set():
                return
            with self._lock:
                got_udp = any(
                    e.get("discovery_method") == "udp"
                    for e in self._servers.values()
                )
            if got_udp:
                return
            try:
                self.active_scan()
            except Exception:
                pass
        threading.Thread(target=runner, daemon=True, name="helen-admin-auto-escalate").start()


# Router manager — UPnP-IGD / NAT-PMP / admin-profile coordinator.
# Importing here so the module is bundled by PyInstaller via admin_app.
from admin_app.router import RouterManager  # noqa: E402


# ── JS bridge exposed to the admin HTML via webview js_api ─


class AdminApi:
    """Surface a minimal, explicit API to the dashboard JS."""

    def __init__(
        self,
        controller: ServerController,
        autostart: AutostartManager,
        discovery: LanDiscovery,
        router: RouterManager,
    ) -> None:
        self._c = controller
        self._auto = autostart
        self._disc = discovery
        self._router = router

    # Prefixed with `server_` so the dashboard is unambiguous in its intent.
    def server_status(self) -> dict:
        return self._c.status()

    def server_start(self) -> dict:
        return self._c.start()

    def server_stop(self) -> dict:
        return self._c.stop()

    def server_restart(self) -> dict:
        return self._c.restart()

    def server_health(self) -> dict | None:
        return _fetch_health()

    def autostart_state(self) -> dict:
        return {"enabled": self._auto.is_enabled()}

    def autostart_toggle(self) -> dict:
        ok = self._auto.toggle()
        return {"ok": ok, "enabled": self._auto.is_enabled()}

    # ── LAN discovery bridge ────────────────────────────────
    def discovery_list(self) -> list[dict]:
        return self._disc.snapshot()

    def discovery_scan_once(self) -> dict:
        # UDP discovery is passive (listens continuously); this just nudges
        # verification so the UI updates quickly after a user click.
        try:
            with self._disc._lock:
                for entry in self._disc._servers.values():
                    entry["_last_check"] = 0
        except Exception:
            pass
        return {"ok": True}

    def discovery_add_manual(self, url: str) -> dict:
        entry = self._disc.add_manual(url)
        return {"ok": entry is not None, "entry": entry}

    def discovery_active_scan(self) -> dict:
        """Force-scan every host on every local /24 for Helen servers.
        Use this when UDP broadcast is blocked by firewall/guest WiFi.
        Runs on a worker thread (pywebview marshals this call off the UI
        thread) so the UI stays responsive during the ~1-2s sweep.
        """
        return self._disc.active_scan()

    # ── Router management bridge ─────────────────────────
    # UPnP-IGD + NAT-PMP + optional admin credentials let the dashboard
    # coerce the router into cooperating (port mappings, AP-isolation
    # tuning via brand profiles). Credentials are stored DPAPI-encrypted
    # so they never hit disk in plaintext.

    def router_detect(self) -> dict:
        snap = self._router.detect()
        return {k: v for k, v in snap.__dict__.items()}

    def router_add_mapping(
        self, port: int, protocol: str = "TCP", description: str = "Helen",
        lease_seconds: int = 0,
    ) -> dict:
        return self._router.add_mapping(
            port=int(port), protocol=protocol,
            description=description, lease_seconds=int(lease_seconds),
        )

    def router_remove_mapping(self, port: int, protocol: str = "TCP") -> dict:
        return self._router.remove_mapping(int(port), protocol)

    def router_save_credentials(
        self, host: str, username: str, password: str, brand: str = "",
    ) -> dict:
        return self._router.save_credentials(host, username, password, brand)

    def router_credentials_status(self) -> dict:
        return self._router.credentials_status()

    def router_clear_credentials(self) -> dict:
        return self._router.clear_credentials()

    def router_apply_profile(self, action: str) -> dict:
        return self._router.apply_known_profile(action)

    # ── Remote-mode persistence ───────────────────────────
    # Dashboard writes the operator's picked base URL here so the *next*
    # Helen-Admin launch knows to skip the local-server autostart. The file
    # is plain text, one URL per line — no schema, no lock, no migrations.
    def remote_pref_save(self, base_url: str) -> dict:
        try:
            path = _exe_dir() / "helen-admin.remote-pref"
            path.write_text(str(base_url or "").strip(), encoding="utf-8")
            return {"ok": True, "path": str(path)}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def remote_pref_clear(self) -> dict:
        try:
            path = _exe_dir() / "helen-admin.remote-pref"
            if path.exists():
                path.unlink()
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def app_info(self) -> dict:
        return {
            "app": "Helen Admin",
            "window_title": WINDOW_TITLE,
            "server_port": SERVER_PORT,
            "static_port": STATIC_PORT,
            "frozen": getattr(sys, "frozen", False),
            "autostart": self._auto.is_enabled(),
            "can_spawn_server": server_exe_path() is not None,
        }


# ── Background health broadcaster ─────────────────────────


def _start_health_broadcaster(window) -> threading.Thread:
    """Push periodic `helen:health` CustomEvents into the page JS."""

    def loop() -> None:
        while True:
            try:
                health = _fetch_health()
                status = {
                    "listening": _port_listening(SERVER_PORT),
                    "health": health,
                }
                payload = json.dumps(status).replace("\\", "\\\\").replace("'", "\\'")
                script = (
                    "window.dispatchEvent(new CustomEvent('helen:health', "
                    f"{{detail: JSON.parse('{payload}')}}));"
                )
                window.evaluate_js(script)
            except Exception:
                # evaluate_js can throw during window teardown; swallow silently.
                pass
            time.sleep(HEALTH_POLL_INTERVAL_SEC)

    t = threading.Thread(target=loop, daemon=True, name="helen-health-broadcaster")
    t.start()
    return t


# ── System tray integration ───────────────────────────────


def _tray_icon_image():
    """Generate a tray icon in-memory — avoids shipping a .ico asset."""
    from PIL import Image, ImageDraw  # local import — only needed if tray runs

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded blue square with a white "H" — instantly recognisable.
    d.rounded_rectangle((4, 4, size - 4, size - 4), radius=12, fill=(76, 194, 255, 255))
    # Two verticals and a crossbar for "H"
    d.rectangle((20, 16, 26, 48), fill=(8, 18, 38, 255))
    d.rectangle((38, 16, 44, 48), fill=(8, 18, 38, 255))
    d.rectangle((20, 29, 44, 35), fill=(8, 18, 38, 255))
    return img


class TrayController:
    """Drive the Windows tray icon and its menu in a dedicated thread."""

    def __init__(
        self,
        controller: ServerController,
        autostart: AutostartManager,
        window_ref: dict,
        on_quit,
    ) -> None:
        self._c = controller
        self._auto = autostart
        self._window_ref = window_ref  # indirection so launch() can set it late
        self._on_quit = on_quit
        self._icon = None
        self._thread: threading.Thread | None = None

    # ── Menu actions ────────────────────────────────────
    def _win(self):
        return self._window_ref.get("win")

    def _show_window(self, *_):
        win = self._win()
        if win is None:
            return
        try:
            win.show()
            try:
                win.restore()
            except Exception:
                pass
        except Exception:
            pass

    def _hide_window(self, *_):
        win = self._win()
        if win is None:
            return
        try:
            win.hide()
        except Exception:
            pass

    def _server_start(self, *_):
        threading.Thread(target=self._c.start, daemon=True).start()

    def _server_stop(self, *_):
        threading.Thread(target=self._c.stop, daemon=True).start()

    def _server_restart(self, *_):
        threading.Thread(target=self._c.restart, daemon=True).start()

    def _toggle_autostart(self, *_):
        self._auto.toggle()
        self._refresh_menu()

    def _quit(self, *_):
        try:
            if self._icon is not None:
                self._icon.visible = False
                self._icon.stop()
        except Exception:
            pass
        self._on_quit()

    # ── Menu + lifecycle ────────────────────────────────
    def _build_menu(self):
        import pystray

        def is_listening(_item):
            return _port_listening(SERVER_PORT)

        def not_listening(_item):
            return not _port_listening(SERVER_PORT)

        def autostart_checked(_item):
            return self._auto.is_enabled()

        return pystray.Menu(
            pystray.MenuItem("Show Helen Admin", self._show_window, default=True),
            pystray.MenuItem("Hide window", self._hide_window),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start server", self._server_start, enabled=not_listening),
            pystray.MenuItem("Stop server", self._server_stop, enabled=is_listening),
            pystray.MenuItem("Restart server", self._server_restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows",
                self._toggle_autostart,
                checked=autostart_checked,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Helen Admin", self._quit),
        )

    def _refresh_menu(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def _periodic_refresh(self) -> None:
        """Re-evaluate menu item enabled/checked flags so they track reality."""
        while self._icon is not None and self._icon.visible:
            self._refresh_menu()
            time.sleep(HEALTH_POLL_INTERVAL_SEC)

    def start(self) -> None:
        import pystray

        self._icon = pystray.Icon(
            "helen-admin",
            _tray_icon_image(),
            TRAY_TITLE,
            menu=self._build_menu(),
        )

        def _run():
            threading.Thread(target=self._periodic_refresh, daemon=True).start()
            # run_detached would return immediately; we want a dedicated thread
            # so pystray owns its message pump without blocking webview.start().
            self._icon.run()

        self._thread = threading.Thread(target=_run, daemon=True, name="helen-tray")
        self._thread.start()


# ── Main entry ────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Helen-Admin", add_help=False)
    p.add_argument("--hidden", action="store_true", help="Start minimised to tray.")
    p.add_argument(
        "--remote", action="store_true",
        help="LAN mode — don't spawn a local Helen-Server; connect to a LAN server via the UI.",
    )
    p.add_argument(
        "--no-autostart-server", action="store_true",
        help="Don't auto-start the bundled Helen-Server at launch (user can still start it from the UI).",
    )
    p.add_argument(
        "--expose-on-lan", action="store_true",
        help="Bind the admin dashboard's static HTTP on 0.0.0.0:5173 so any browser on the LAN can open it. Default is loopback only.",
    )
    p.add_argument("--help", action="help")
    # Use parse_known_args so PyInstaller-injected flags don't crash us.
    args, _unknown = p.parse_known_args()
    return args


def launch() -> None:
    args = _parse_args()

    # Single-instance guard — if another Helen-Admin already owns the mutex,
    # quit silently instead of spawning another server on a busy port.
    guard = SingleInstanceGuard(SINGLE_INSTANCE_MUTEX)
    if not guard.acquired:
        return
    atexit.register(guard.release)

    admin_dir = resource_path(ADMIN_FOLDER)
    if not Path(admin_dir, "index.html").exists():
        raise SystemExit(f"admin/index.html not found at: {admin_dir}")

    controller = ServerController()
    autostart = AutostartManager()
    discovery = LanDiscovery()
    discovery.start()
    router = RouterManager()
    # Run router detection off the UI thread so a slow SSDP reply never
    # stalls Helen-Admin launch. Results populate on the next dashboard poll.
    threading.Thread(target=router.detect, daemon=True, name="helen-admin-router-detect").start()

    # If UDP 41234 is occupied (common when Helen-Server runs on the same box),
    # passive discovery is blind. Seed the list by probing localhost so the
    # picker still shows the local server on same-machine installs.
    def _seed_localhost() -> None:
        time.sleep(1.0)
        try:
            discovery.add_manual(f"http://127.0.0.1:{SERVER_PORT}")
        except Exception:
            pass
    threading.Thread(target=_seed_localhost, daemon=True).start()

    # Mandatory-connection guarantee: if UDP broadcasts never arrive (firewall,
    # guest network, multicast disabled), silently escalate to an active
    # subnet scan after a short grace period so the picker still populates.
    discovery.auto_escalate_if_silent(wait_sec=5.0)

    # Local-server autostart is opt-out: skip when the operator explicitly
    # asked for LAN/remote mode, when the bundled exe isn't shipped alongside
    # the admin (LAN-only distributions), or when 3000 is already taken (e.g.
    # another admin instance or the server was started independently).
    #
    # Remote-mode memory: if the operator previously switched the dashboard
    # to a non-local server URL, persist that choice to a tiny state file
    # beside the admin exe so the NEXT launch doesn't spawn a local server
    # that would just confuse things. The UI clears this file when the
    # operator hits "reset to default".
    _remote_state = _exe_dir() / "helen-admin.remote-pref"
    persisted_remote = False
    try:
        if _remote_state.exists():
            saved = _remote_state.read_text(encoding="utf-8").strip()
            # Treat any non-local saved URL as "remote mode".
            if saved and not saved.startswith(("http://localhost", "http://127.")):
                persisted_remote = True
    except OSError:
        pass

    remote_mode = args.remote or args.no_autostart_server or persisted_remote
    if not remote_mode and server_exe_path() and not _port_listening(SERVER_PORT):
        controller.start()

    # Serve the admin HTML over HTTP. Default bind = loopback (127.0.0.1)
    # so the pywebview shell is the only visible UI. Pass --expose-on-lan
    # to rebind on 0.0.0.0 so any browser on the LAN can open the dashboard
    # directly (useful when the operator wants to drive the server from
    # a tablet or another PC on the same router).
    if _port_free(STATIC_PORT):
        threading.Thread(
            target=_start_static_server,
            args=(admin_dir, STATIC_PORT),
            kwargs={"expose_on_lan": args.expose_on_lan},
            daemon=True,
        ).start()
        # Give the server a moment to bind.
        time.sleep(0.3)

    api = AdminApi(controller, autostart, discovery, router)

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://localhost:{STATIC_PORT}/",
        width=1320,
        height=860,
        min_size=(960, 640),
        resizable=True,
        confirm_close=False,
        background_color=BG_COLOR,
        hidden=args.hidden,
        js_api=api,
    )

    # Indirection container — the tray thread keeps a reference to this dict
    # so actions triggered before launch() returns still see the live window.
    window_ref: dict = {"win": window}

    # Tray quit callback — terminates the webview loop, which causes launch()
    # to fall through to the final cleanup.
    def _quit_from_tray() -> None:
        try:
            window.destroy()
        except Exception:
            pass

    tray = TrayController(controller, autostart, window_ref, _quit_from_tray)

    def _on_loaded() -> None:
        _start_health_broadcaster(window)

    def _on_closing() -> bool:
        """Hide to tray instead of closing — keep the server running.

        Returning False aborts the close. The user can fully quit via the
        tray menu's "Quit Helen Admin" item.
        """
        try:
            window.hide()
        except Exception:
            pass
        return False

    window.events.loaded += _on_loaded
    window.events.closing += _on_closing

    # Start the tray before webview.start so the icon appears even on --hidden.
    tray.start()

    webview.start(debug=False)

    # webview.start returns when window.destroy() is called (Quit from tray).
    discovery.stop()
    controller.stop()
    guard.release()


if __name__ == "__main__":
    launch()
