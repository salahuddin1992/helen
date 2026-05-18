"""
Helen-Server — windowed desktop wrapper.

Same backend (FastAPI + uvicorn) as the console-mode Helen-Server, but now
with a visible operator UI:

  * pywebview window showing live server status, port, uptime, connected
    sessions, and a rolling tail of the server log.
  * System tray icon with Start / Stop / Restart / Open Admin / Quit.
  * JS bridge (`window.pywebview.api.*`) so the dashboard can query status
    and trigger lifecycle actions without hitting the HTTP API.

The uvicorn server runs in a background thread *inside this process* —
there is no subprocess. If the user wants the old headless behaviour,
they can launch `run.py` directly; this wrapper is strictly additive.
"""

from __future__ import annotations

import argparse
import atexit
import ctypes
import http.server
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

# run.py's preamble creates data/logs dirs, picks a free port, copies .env.
# Import it BEFORE any app.* imports so env is primed identically to the
# headless entrypoint.
_SELF_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = _SELF_DIR.parent
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(sys.executable))
else:
    sys.path.insert(0, str(_PROJECT_ROOT))

import webview  # noqa: E402

# ── Configuration ─────────────────────────────────────────
STATIC_PORT = 5175  # pywebview loads UI from http://127.0.0.1:5175/
WINDOW_TITLE = "Helen — Server Console"
TRAY_TITLE = "Helen Server"
BG_COLOR = "#0b1220"
LOG_TAIL_CAPACITY = 500

SINGLE_INSTANCE_MUTEX = "Global\\HelenServer.SingleInstance.v1"

# Resolved at startup from run.py preamble / settings.
_START_TIME = time.time()


# ── Resource resolution ───────────────────────────────────

def _frozen_base() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _SELF_DIR


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _SELF_DIR


def _ui_dir() -> str:
    """Locate server_app/ui — bundled or running from source."""
    base = _frozen_base()
    for c in (base / "server_app" / "ui", base / "ui", _SELF_DIR / "ui"):
        if (c / "index.html").exists():
            return str(c)
    return str(_SELF_DIR / "ui")


def _admin_exe_candidates() -> list[Path]:
    """Paths the operator might have Helen-Admin.exe at."""
    base = _exe_dir()
    return [
        base / "Helen-Admin.exe",
        base.parent / "Helen-Admin" / "Helen-Admin.exe",
        base.parent / "dist" / "Helen-Admin" / "Helen-Admin.exe",
    ]


# ── Single-instance guard ─────────────────────────────────

class SingleInstanceGuard:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        self.acquired = False
        self._handle = None
        if sys.platform != "win32":
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
            self.acquired = True

    def release(self) -> None:
        if self._handle and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


# ── In-memory log tail ────────────────────────────────────

@dataclass
class LogRecord:
    seq: int
    ts: float
    level: str
    msg: str


class TailHandler(logging.Handler):
    """Keep the most recent N log records for the UI to display."""

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._buf: deque[LogRecord] = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self._lock:
            self._seq += 1
            self._buf.append(
                LogRecord(
                    seq=self._seq,
                    ts=record.created,
                    level=record.levelname,
                    msg=msg,
                )
            )

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [
                {"seq": r.seq, "ts": r.ts, "level": r.level, "msg": r.msg}
                for r in self._buf
                if r.seq > seq
            ]


_TAIL = TailHandler(LOG_TAIL_CAPACITY)
_TAIL.setFormatter(logging.Formatter("%(name)s | %(message)s"))


def _install_log_capture() -> None:
    """Attach TailHandler to root + uvicorn loggers so both app & server lines flow in."""
    root = logging.getLogger()
    # Don't override the existing level if one was already set by the app.
    if root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(_TAIL)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "app"):
        lg = logging.getLogger(name)
        lg.addHandler(_TAIL)


# ── Uvicorn embedded server controller ────────────────────

class EmbeddedServer:
    """Run uvicorn in a daemon thread; allow graceful restart without exiting."""

    def __init__(self) -> None:
        self._server = None  # uvicorn.Server
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._host = "0.0.0.0"
        self._port = 3000
        self._started_at: float | None = None
        self._config_err: str | None = None

    def _build_config(self):
        import uvicorn
        from app.core.config import get_settings
        from app.main import app
        settings = get_settings()
        self._host = settings.HOST
        # Honour $PORT set by run.py preamble (auto-scanned free port).
        env_port = os.environ.get("PORT")
        self._port = int(env_port) if env_port and env_port.isdigit() else settings.PORT

        ssl_kwargs: dict = {}
        if getattr(settings, "HTTPS_ENABLED", False):
            try:
                from app.core.tls import ensure_certificate
                certfile, keyfile = settings.ssl_paths
                extra = [s for s in settings.SSL_EXTRA_SANS.split(",") if s.strip()]
                ensure_certificate(certfile, keyfile, extra_sans=extra)
                ssl_kwargs = {
                    "ssl_certfile": str(certfile),
                    "ssl_keyfile": str(keyfile),
                }
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "tls_setup_failed_falling_back_to_http", exc_info=e
                )

        return uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level=settings.LOG_LEVEL.lower(),
            ws="auto",
            access_log=False,
            backlog=int(os.environ.get("UVICORN_BACKLOG", "8192")),
            timeout_keep_alive=30,
            **ssl_kwargs,
        )

    def start(self) -> None:
        import uvicorn
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            try:
                config = self._build_config()
            except Exception as e:
                self._config_err = str(e)
                logging.getLogger(__name__).exception("server_config_failed")
                return
            self._server = uvicorn.Server(config)

            def _run() -> None:
                try:
                    self._server.run()
                except Exception:
                    logging.getLogger(__name__).exception("uvicorn_thread_crashed")

            self._thread = threading.Thread(
                target=_run, daemon=True, name="helen-uvicorn"
            )
            self._started_at = time.time()
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal uvicorn to shut down and wait up to `timeout` seconds."""
        with self._lock:
            srv = self._server
            thr = self._thread
        if srv is not None:
            srv.should_exit = True
        if thr is not None:
            thr.join(timeout=timeout)
        with self._lock:
            self._server = None
            self._thread = None
            self._started_at = None

    def restart(self) -> None:
        self.stop()
        # Brief cooldown so the listen socket is released before rebind.
        time.sleep(0.8)
        self.start()

    # ── Status queries ────────────────────────────────────

    def listening(self) -> bool:
        try:
            host = "127.0.0.1" if self._host in ("0.0.0.0", "::", "") else self._host
            with socket.create_connection((host, self._port), timeout=0.5):
                return True
        except OSError:
            return False

    def snapshot(self) -> dict:
        with self._lock:
            started = self._started_at
            alive = bool(self._thread and self._thread.is_alive())
        return {
            "listening": self.listening(),
            "alive": alive,
            "host": self._host,
            "port": self._port,
            "pid": os.getpid(),
            "uptime_sec": (time.time() - started) if started else 0,
            "config_error": self._config_err,
            "frozen": bool(getattr(sys, "frozen", False)),
            "started_at": started,
        }


# ── Static HTTP for UI ────────────────────────────────────

def _start_static_server(directory: str, port: int) -> None:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def log_message(self, fmt, *args):  # noqa: ARG002
            pass

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        httpd.serve_forever()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


# ── JS bridge ─────────────────────────────────────────────

class ServerApi:
    """Exposed to the dashboard via webview `js_api`."""

    def __init__(self, server: EmbeddedServer, on_quit) -> None:
        self._s = server
        self._on_quit = on_quit

    def status(self) -> dict:
        snap = self._s.snapshot()
        # Enrich with in-process live stats — presence + rate-limiter buckets.
        # Both are sync, in-memory, cheap; safe to call every poll tick.
        snap["online_users"] = self._online_users()
        snap["rate_limit_buckets"] = self._bucket_count()
        return snap

    @staticmethod
    def _online_users() -> int | None:
        try:
            from app.services.presence_service import presence_service
            return presence_service.get_online_count()
        except Exception:
            return None

    @staticmethod
    def _bucket_count() -> int | None:
        try:
            from app.core.middleware import global_rate_limiter
            return global_rate_limiter.size()
        except Exception:
            return None

    def restart(self) -> dict:
        """Process-level restart — spawn a fresh instance, then exit.

        In-process restart can't work reliably: FastAPI's lifespan is a
        single-shot async generator, `asyncio.Lock()` instances created at
        module import bind to the first event loop, and UDP/mDNS sockets
        aren't idempotent on re-bind. The only reliable "restart" for a
        server with this much stateful startup is a process swap.
        """
        def _do():
            # Let the RPC response reach the UI before we tear anything down.
            time.sleep(0.3)
            try:
                self._s.stop()
            except Exception:
                logging.getLogger(__name__).exception("restart_stop_failed")
            # Release the single-instance mutex so the child can acquire it.
            try:
                if _GUARD is not None:
                    _GUARD.release()
            except Exception:
                pass
            # Build the command line for the successor process.
            if getattr(sys, "frozen", False):
                cmd = [sys.executable]
            else:
                cmd = [sys.executable, "-m", "server_app.main"]
            # Spawn fully detached — no shared stdio/handles, not a child of
            # this dying process tree.
            flags = 0
            if sys.platform == "win32":
                DETACHED_PROCESS = 0x00000008
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(
                    cmd,
                    cwd=str(_PROJECT_ROOT),
                    creationflags=flags,
                    close_fds=True,
                )
            except Exception:
                logging.getLogger(__name__).exception("restart_spawn_failed")
            # Ask pywebview to exit; fall back to hard exit if it won't.
            try:
                self._on_quit()
            except Exception:
                pass
            time.sleep(2.0)
            os._exit(0)

        threading.Thread(target=_do, daemon=True).start()
        return {"ok": True, "strategy": "process"}

    def stop(self) -> dict:
        if not self._s.listening() and not getattr(self._s, "_thread", None):
            return {"ok": True, "already_stopped": True}
        threading.Thread(target=self._s.stop, daemon=True).start()
        return {"ok": True}

    def start(self) -> dict:
        # Already-running is a common footgun — tell the UI explicitly so
        # it can show a "running" toast rather than a silent no-op.
        if self._s.listening():
            return {"ok": True, "already_running": True}
        threading.Thread(target=self._s.start, daemon=True).start()
        return {"ok": True}

    def recent_logs(self, since_seq: int = 0) -> list[dict]:
        try:
            since_seq = int(since_seq)
        except (TypeError, ValueError):
            since_seq = 0
        return _TAIL.since(since_seq)

    def backup_state(self) -> dict:
        """Snapshot of auto-backup scheduler — for the dashboard card.

        Read-only; triggering a run is done from the admin panel so it uses
        the same audit-logged code path as any other operator action.
        """
        try:
            from app.services import backup_scheduler
            return backup_scheduler.get_state().snapshot()
        except Exception as e:
            return {"error": str(e)}

    def open_url(self, url: str) -> dict:
        """Open an arbitrary URL in the system default browser.

        pywebview doesn't support `window.open(url, '_blank')` for external
        links — there's no tab chrome and no window manager hook. We route
        those clicks here so `/docs` and `/api/health` open in the user's
        real browser (Chrome/Edge/Firefox) rather than silently fail.

        Only allows http(s) to localhost / 127.0.0.1 / <LAN IP>:PORT so a
        compromised renderer can't use this as a generic URL launcher.
        """
        import webbrowser
        try:
            u = str(url or "").strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                return {"ok": False, "error": "invalid_scheme"}
            # Parse authority: http(s)://<host>[:port]/...
            rest = u.split("://", 1)[1]
            host_port = rest.split("/", 1)[0]
            host = host_port.split(":")[0]
            allowed_hosts = {"127.0.0.1", "localhost", "::1"}
            try:
                from app.services.discovery_service import get_lan_ip
                lan = get_lan_ip()
                if lan:
                    allowed_hosts.add(lan)
            except Exception:
                pass
            if host not in allowed_hosts:
                return {"ok": False, "error": f"host_not_allowed:{host}"}
            webbrowser.open(u, new=2)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_logs_folder(self) -> dict:
        log_dir = Path(os.environ.get("LOG_DIR") or (_exe_dir() / "logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(log_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(log_dir)])
            else:
                subprocess.Popen(["xdg-open", str(log_dir)])
            return {"ok": True, "path": str(log_dir)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_admin(self) -> dict:
        """Open the admin dashboard.

        Priority:
          1. If Helen-Admin's static HTTP server (5173) is already listening —
             just open that URL in the browser. Avoids fighting the admin's
             single-instance mutex when Helen-Admin.exe is already running.
          2. Else spawn Helen-Admin.exe if we can find it.
          3. Else open the server's /docs as a last resort.
        """
        import webbrowser

        # 1) Existing Helen-Admin static server → open its page
        ADMIN_STATIC_PORT = 5173
        try:
            with socket.create_connection(("127.0.0.1", ADMIN_STATIC_PORT), timeout=0.3):
                url = f"http://127.0.0.1:{ADMIN_STATIC_PORT}/"
                webbrowser.open(url)
                return {"ok": True, "opened_url": url, "via": "running_admin"}
        except OSError:
            pass

        # 2) Spawn Helen-Admin.exe — but first check whether one is already
        # running; if so, the new process would just hit the mutex and exit
        # silently which is confusing. Tell the UI instead.
        if sys.platform == "win32":
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq Helen-Admin.exe", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=3,
                )
                if "Helen-Admin.exe" in (out.stdout or ""):
                    return {
                        "ok": False,
                        "already_running": True,
                        "error": "Helen-Admin is already running — check the system tray.",
                    }
            except Exception:
                pass

        for c in _admin_exe_candidates():
            if c.exists():
                try:
                    subprocess.Popen([str(c)], cwd=str(c.parent))
                    return {"ok": True, "launched": str(c)}
                except Exception as e:
                    return {"ok": False, "error": str(e)}

        # 3) Fallback — open the server's own Swagger docs
        snap = self._s.snapshot()
        url = f"http://127.0.0.1:{snap['port']}/docs"
        try:
            webbrowser.open(url)
            return {"ok": True, "opened_url": url, "via": "docs_fallback"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def quit_app(self) -> dict:
        threading.Thread(target=self._on_quit, daemon=True).start()
        return {"ok": True}


# ── Tray icon ─────────────────────────────────────────────

def _tray_icon_image():
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Green rounded square + white "S" — distinguishes from the admin tray icon.
    d.rounded_rectangle((4, 4, size - 4, size - 4), radius=12, fill=(52, 211, 153, 255))
    d.rectangle((20, 16, 44, 22), fill=(8, 38, 28, 255))
    d.rectangle((20, 30, 44, 36), fill=(8, 38, 28, 255))
    d.rectangle((20, 44, 44, 50), fill=(8, 38, 28, 255))
    d.rectangle((20, 16, 26, 36), fill=(8, 38, 28, 255))
    d.rectangle((38, 30, 44, 50), fill=(8, 38, 28, 255))
    return img


class TrayController:
    def __init__(self, server: EmbeddedServer, window_ref: dict, on_quit) -> None:
        self._s = server
        self._window_ref = window_ref
        self._on_quit = on_quit
        self._icon = None

    def _win(self):
        return self._window_ref.get("win")

    def _show(self, *_):
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

    def _hide(self, *_):
        win = self._win()
        if win is None:
            return
        try:
            win.hide()
        except Exception:
            pass

    def _restart(self, *_):
        threading.Thread(target=self._s.restart, daemon=True).start()

    def _stop(self, *_):
        threading.Thread(target=self._s.stop, daemon=True).start()

    def _start(self, *_):
        threading.Thread(target=self._s.start, daemon=True).start()

    def _quit(self, *_):
        try:
            if self._icon is not None:
                self._icon.visible = False
                self._icon.stop()
        except Exception:
            pass
        self._on_quit()

    def _build_menu(self):
        import pystray

        def is_listening(_item):
            return self._s.listening()

        def not_listening(_item):
            return not self._s.listening()

        return pystray.Menu(
            pystray.MenuItem("Show window", self._show, default=True),
            pystray.MenuItem("Hide window", self._hide),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start server", self._start, enabled=not_listening),
            pystray.MenuItem("Stop server", self._stop, enabled=is_listening),
            pystray.MenuItem("Restart server", self._restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Helen Server", self._quit),
        )

    def start(self) -> None:
        import pystray

        self._icon = pystray.Icon(
            "helen-server",
            _tray_icon_image(),
            TRAY_TITLE,
            menu=self._build_menu(),
        )

        def _run():
            self._icon.run()

        threading.Thread(target=_run, daemon=True, name="helen-server-tray").start()


# ── Entry ─────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Helen-Server", add_help=False)
    p.add_argument("--hidden", action="store_true", help="Start minimised to tray.")
    p.add_argument("--help", action="help")
    args, _unknown = p.parse_known_args()
    return args


def _run_preamble() -> None:
    """Mirror run.py's env setup so an embedded launch lands in the same state."""
    # Ensure required runtime dirs exist.
    base = os.environ.get(
        "COMMCLIENT_DATA_DIR",
        str(_PROJECT_ROOT if not getattr(sys, "frozen", False) else _exe_dir()),
    )
    for sub in ("data", "data/backups", "data/uploads", "data/avatars", "files", "logs"):
        try:
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        except OSError:
            pass

    # Auto-copy .env.example → .env on first run.
    env_path = os.path.join(base, ".env")
    env_example = os.path.join(base, ".env.example")
    if not os.path.exists(env_path) and os.path.exists(env_example):
        try:
            import shutil
            shutil.copy2(env_example, env_path)
        except OSError:
            pass

    # Pick a free port if $PORT isn't already set.
    if not os.environ.get("PORT"):
        for port in range(3000, 3011):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("0.0.0.0", port))
                    os.environ["PORT"] = str(port)
                    break
                except OSError:
                    continue

    # compat shim (same as run.py)
    try:
        import compat  # noqa: F401
    except Exception:
        pass


_GUARD: "SingleInstanceGuard | None" = None  # released by restart before exec


def launch() -> None:
    global _GUARD
    args = _parse_args()

    guard = SingleInstanceGuard(SINGLE_INSTANCE_MUTEX)
    if not guard.acquired:
        return
    atexit.register(guard.release)
    _GUARD = guard

    _run_preamble()
    _install_log_capture()

    server = EmbeddedServer()
    server.start()

    ui_dir = _ui_dir()
    if _port_free(STATIC_PORT):
        threading.Thread(
            target=_start_static_server,
            args=(ui_dir, STATIC_PORT),
            daemon=True,
        ).start()
        time.sleep(0.3)

    window_ref: dict = {"win": None}

    def _quit_app() -> None:
        win = window_ref.get("win")
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    api = ServerApi(server, _quit_app)

    window = webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{STATIC_PORT}/",
        width=980,
        height=720,
        min_size=(720, 520),
        resizable=True,
        confirm_close=False,
        background_color=BG_COLOR,
        hidden=args.hidden,
        js_api=api,
    )
    window_ref["win"] = window

    def _on_closing() -> bool:
        # Hide to tray instead of closing so the server keeps running.
        try:
            window.hide()
        except Exception:
            pass
        return False

    window.events.closing += _on_closing

    tray = TrayController(server, window_ref, _quit_app)
    tray.start()

    webview.start(debug=False)

    # Window destroyed — stop the embedded server cleanly before exit.
    server.stop()
    guard.release()


if __name__ == "__main__":
    launch()
