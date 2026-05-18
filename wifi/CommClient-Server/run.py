"""
Server launcher — run this file to start the CommClient server.

Works in both development (python run.py) and production (PyInstaller frozen exe)
on any installed Python version (3.8 → 3.13+).

Usage:
  Development: python run.py
  Production:  CommClient-Server.exe (built by PyInstaller)

Behavior:
  • Auto-installs distutils shim on Python 3.12+ (top of file).
  • Auto-creates data/ subdirs and copies .env.example -> .env if needed.
  • Auto-detects a free port in 3000-3010 (unless PORT is set).
  • Imports compat.py for the wider version polyfill suite.
  • Falls back gracefully if optional dependencies are not installed.
"""

# ── Auto-compatibility preamble (runs BEFORE any other imports) ──
import sys, os

# Auto-compatibility layer for Python 3.12+ (distutils removed)
if sys.version_info >= (3, 12):
    try:
        import distutils  # noqa: F401
    except ImportError:
        try:
            import setuptools._distutils as _distutils
            sys.modules['distutils'] = _distutils
        except ImportError:
            pass

# Auto-create required directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
for d in ['data', 'data/backups', 'data/uploads', 'data/avatars']:
    os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)

# Auto-create .env if missing
env_path = os.path.join(BASE_DIR, '.env')
env_example = os.path.join(BASE_DIR, '.env.example')
if not os.path.exists(env_path) and os.path.exists(env_example):
    import shutil
    shutil.copy2(env_example, env_path)

# Load .env into os.environ before any module reads from it.
# Pydantic Settings reads .env on its own, but plain code paths
# (router_client, RouterRequiredMiddleware, mdns_discovery) call
# os.environ.get directly — so we hydrate the process env here.
if os.path.exists(env_path):
    try:
        with open(env_path, 'r', encoding='utf-8') as _envf:
            for _line in _envf:
                _line = _line.strip()
                if not _line or _line.startswith('#') or '=' not in _line:
                    continue
                _k, _v = _line.split('=', 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                # setdefault so explicit os env (set on the command line)
                # always wins over the .env file.
                os.environ.setdefault(_k, _v)
    except Exception:
        pass  # malformed .env is non-fatal; Settings will surface it

# Auto-detect an available port.
#
# Old behaviour: try 3000-3010 only, return the start port even if
# every probe failed. That meant a corporate Windows host where 3000
# was already grabbed by BITS / Windows Update / Splunk would
# "successfully" start with PORT=3000 and then fail to bind in uvicorn,
# leaving the user with a silently-dead service.
#
# New behaviour: probe a much wider range, walk it in 3 tiers, and
# write the FINAL chosen port to a sidecar file so the Electron parent
# can pick it up via IPC.
import socket as _socket_preamble


def _try_bind_preamble(port: int) -> bool:
    try:
        s = _socket_preamble.socket(
            _socket_preamble.AF_INET, _socket_preamble.SOCK_STREAM,
        )
        # SO_REUSEADDR is intentionally OFF — we want the bind to
        # succeed only if nobody else is listening on this port.
        s.bind(('0.0.0.0', port))
        s.close()
        return True
    except OSError:
        return False


def _find_port_preamble() -> int:
    """Walk three port tiers, return the first free port.

    Tier 1: classic 3000-3010 (familiar to existing operators)
    Tier 2: 3011-3099 (still close to default for muscle-memory)
    Tier 3: 50000-50099 (always free on a fresh Windows host)
    """
    for tier_start, tier_end in ((3000, 3010), (3011, 3099),
                                   (50000, 50099)):
        for port in range(tier_start, tier_end + 1):
            if _try_bind_preamble(port):
                return port
    # Last-ditch — let the OS pick anything
    s = _socket_preamble.socket(
        _socket_preamble.AF_INET, _socket_preamble.SOCK_STREAM,
    )
    s.bind(('0.0.0.0', 0))
    port = s.getsockname()[1]
    s.close()
    return port


_chosen_port = int(os.environ.get('PORT', '0')) or _find_port_preamble()
os.environ['PORT'] = str(_chosen_port)

# Side-channel for the Electron parent: write the chosen port so the
# wrapper can read `<DATA_DIR>/.helen-server.port` instead of
# guessing 3000. Best-effort — failure here is harmless.
try:
    _port_file = os.path.join(BASE_DIR, 'data', '.helen-server.port')
    with open(_port_file, 'w', encoding='utf-8') as _f:
        _f.write(str(_chosen_port))
except Exception:
    pass
# ── End auto-compatibility preamble ──

import socket

# Determine if running as frozen PyInstaller bundle
IS_FROZEN = getattr(sys, 'frozen', False)

if IS_FROZEN:
    # PyInstaller sets sys._MEIPASS to the temp extraction directory.
    # For --onedir mode, the exe directory is the bundle root.
    bundle_dir = os.path.dirname(sys.executable)
    sys.path.insert(0, bundle_dir)
else:
    # Development: ensure project root is on path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# IMPORTANT: import compat shim before any third-party / app imports.
# In frozen mode this is also done by rt_hook_compat.py, but importing
# again is a no-op (apply() is idempotent).
try:
    import compat  # noqa: F401
except Exception as _e:  # pragma: no cover
    print(f"[run] WARNING: compat shim failed to load: {_e}", file=sys.stderr)


def _ensure_runtime_dirs() -> None:
    """Create the directories the server expects, if they don't exist."""
    base = os.environ.get(
        "COMMCLIENT_DATA_DIR",
        os.path.dirname(os.path.abspath(__file__)),
    )
    for sub in ("data", "files", "logs"):
        try:
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        except Exception as e:
            print(
                f"[run] WARNING: could not create {sub} dir: {e}",
                file=sys.stderr,
            )


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _resolve_port(host: str, default_port: int) -> int:
    """If $PORT is set, honor it. Otherwise scan 3000-3010."""
    env_port = os.environ.get("PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            print(
                f"[run] WARNING: PORT={env_port!r} is not an int, scanning instead",
                file=sys.stderr,
            )
    bind_host = "0.0.0.0" if host in ("0.0.0.0", "::", "") else host
    for port in range(3000, 3011):
        if _port_is_free(bind_host, port):
            return port
    return default_port


_ensure_runtime_dirs()

import uvicorn  # noqa: E402
from app.core.config import get_settings  # noqa: E402

settings = get_settings()
PORT = _resolve_port(settings.HOST, settings.PORT)
if PORT != settings.PORT:
    print(f"[run] selected free port: {PORT} (default {settings.PORT} unavailable)")
# Make the resolved port visible to the app and to any subprocess that
# inherits our env (the Electron parent reads this back via /api/health).
os.environ["PORT"] = str(PORT)

def _resolve_ssl_kwargs(auto_generate: bool = True) -> dict:
    """Build uvicorn's ssl_* kwargs if HTTPS is enabled, else return {}.

    If ``auto_generate`` is True (the default for the HTTPS-alongside-HTTP
    mode), we'll always try to mint a cert so iPhone Safari and Android
    Chrome can access the camera via ``/pair?t=…`` without the operator
    flipping a flag first. ``helen.local`` is added to SANs automatically
    so scanning a ``https://helen.local:3443/pair?t=…`` QR works too.
    """
    https_enabled = auto_generate or getattr(settings, "HTTPS_ENABLED", False)
    if not https_enabled:
        return {}
    try:
        from app.core.tls import ensure_certificate
        certfile, keyfile = settings.ssl_paths
        extra = [s for s in settings.SSL_EXTRA_SANS.split(",") if s.strip()]
        # Ensure helen.local is covered — the mDNS-advertised hostname
        # that phones discover via the system resolver.
        if "helen.local" not in extra:
            extra.append("helen.local")
        ensure_certificate(certfile, keyfile, extra_sans=extra)
        print(f"[run] HTTPS enabled: cert={certfile} key={keyfile}")
        return {"ssl_certfile": str(certfile), "ssl_keyfile": str(keyfile)}
    except Exception as _e:
        print(
            f"[run] WARNING: HTTPS setup failed: {_e}. Falling back to HTTP-only.",
            file=sys.stderr,
        )
        return {}


def _run_https_sidecar(
    _app_or_str, https_port: int, ssl_kwargs: dict, _backlog: int,
    upstream_port: int,
) -> None:
    """Start a TLS-terminating TCP proxy in a daemon thread.

    Why a proxy instead of a second uvicorn Server:

      Running the FastAPI ``app`` in two uvicorn.Server instances (one
      HTTP, one HTTPS) fires the lifespan startup twice — mDNS
      registration conflicts on UDP 5353, UDP broadcast double-binds
      41234, audit-writer starts twice, etc. Only the first listener's
      startup succeeds cleanly; the second silently aborts and leaves
      its port unbound. The symptom is exactly what we hit: port 3443
      answers, port 3000 doesn't.

      Instead, we keep a single uvicorn serving HTTP on ``upstream_port``
      and put a ~150-line TLS-termination proxy in front of port 3443
      that splices the decrypted TCP bytes straight to the HTTP
      listener. This is protocol-agnostic, so WebSocket upgrades
      (``/socket.io/``) tunnel through correctly.
    """
    import asyncio as _asyncio
    import ssl as _ssl
    import threading as _threading

    certfile = ssl_kwargs.get("ssl_certfile")
    keyfile = ssl_kwargs.get("ssl_keyfile")
    if not certfile or not keyfile:
        return

    def _target() -> None:
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
            # Relax the cipher list so older iOS/Android Safari versions
            # (which still speak TLS 1.2) can negotiate. Still rejects all
            # the known-weak suites.
            try:
                ctx.set_ciphers("DEFAULT:!aNULL:!eNULL:!MD5:!RC4:!3DES")
            except _ssl.SSLError:
                pass

            async def _handle(
                cli_reader: _asyncio.StreamReader,
                cli_writer: _asyncio.StreamWriter,
            ) -> None:
                try:
                    up_r, up_w = await _asyncio.open_connection(
                        "127.0.0.1", upstream_port,
                    )
                except OSError as e:
                    print(f"[run] HTTPS proxy: upstream connect failed: {e}",
                          file=sys.stderr)
                    cli_writer.close()
                    return

                async def _pump(src, dst):
                    try:
                        while True:
                            data = await src.read(65536)
                            if not data:
                                break
                            dst.write(data)
                            await dst.drain()
                    except (ConnectionError, OSError):
                        pass
                    finally:
                        try:
                            dst.close()
                        except Exception:
                            pass

                await _asyncio.gather(
                    _pump(cli_reader, up_w),
                    _pump(up_r, cli_writer),
                    return_exceptions=True,
                )

            async def _serve():
                server = await _asyncio.start_server(
                    _handle, host=settings.HOST, port=https_port, ssl=ctx,
                )
                async with server:
                    await server.serve_forever()

            loop.run_until_complete(_serve())
        except Exception as e:  # pragma: no cover
            print(f"[run] HTTPS sidecar crashed: {e}", file=sys.stderr)

    t = _threading.Thread(target=_target, name="helen-https-sidecar", daemon=True)
    t.start()
    print(f"[run] HTTPS sidecar: TLS-terminating on :{https_port} -> HTTP :{upstream_port}")


if __name__ == "__main__":
    # Larger accept queue so 500-1000 concurrent socket handshakes don't
    # overflow the kernel backlog (default 2048). Also bump ws frame size
    # slightly so large presence snapshots don't trip the default 16k cap.
    ACCEPT_BACKLOG = int(os.environ.get("UVICORN_BACKLOG", "8192"))

    # HTTPS is on by default so mobile browsers can pair — iPhone Safari
    # and Android Chrome refuse getUserMedia() on plain http://LAN-IP.
    # Operators can disable by setting HELEN_HTTPS_DISABLED=1.
    HTTPS_DISABLED = os.environ.get("HELEN_HTTPS_DISABLED", "").lower() in {
        "1", "true", "yes", "on",
    }
    SSL_KWARGS = {} if HTTPS_DISABLED else _resolve_ssl_kwargs(auto_generate=True)
    HTTPS_PORT = int(os.environ.get("HELEN_HTTPS_PORT", "3443"))

    # In frozen mode, disable reload (no source files to watch)
    # and use app object directly (import string won't resolve in frozen)
    if IS_FROZEN:
        from app.main import app
        # Launch TLS-terminating proxy first; it forwards into HTTP on `PORT`.
        if SSL_KWARGS:
            _run_https_sidecar(app, HTTPS_PORT, SSL_KWARGS, ACCEPT_BACKLOG, PORT)
        uvicorn.run(
            app,
            host=settings.HOST,
            port=PORT,
            reload=False,
            log_level=settings.LOG_LEVEL.lower(),
            ws="auto",
            access_log=False,
            backlog=ACCEPT_BACKLOG,
            timeout_keep_alive=30,
        )
    else:
        if SSL_KWARGS:
            _run_https_sidecar("app.main:app", HTTPS_PORT, SSL_KWARGS, ACCEPT_BACKLOG, PORT)
        uvicorn.run(
            "app.main:app",
            host=settings.HOST,
            port=PORT,
            reload=settings.DEBUG,
            log_level=settings.LOG_LEVEL.lower(),
            ws="auto",
            access_log=settings.DEBUG,
            backlog=ACCEPT_BACKLOG,
            timeout_keep_alive=30,
        )
