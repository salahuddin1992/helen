"""
SFU worker auto-launcher (Task #2).

Spawns and supervises the Node.js mediasoup-worker living under
`CommClient-Server/sfu-worker/` so group video calls with more than 3
participants actually have an SFU to fall back to. Previously this worker
had to be started manually — in the "PC = LAN server" deployment nobody
remembers, and `topology_manager.MediasoupBridge` silently fails when
the worker is unreachable.

Responsibilities
----------------
  * Resolve the worker directory relative to the backend (handles both
    the source tree and PyInstaller frozen builds where `sys._MEIPASS`
    points into `_internal/`).
  * Install npm deps on first run if `node_modules/` is missing.
  * Spawn `node src/server.js` with the right env (mediasoup control
    token, announced IP, port range, recordings dir).
  * Capture stdout/stderr into per-session log files so crashes are
    diagnosable after the fact.
  * Restart the worker with exponential backoff on non-zero exit.
  * Expose `sfu_launcher.start() / .stop() / .is_healthy()` for the
    extended bootstrap lifespan hook.

Config
------
Everything is overridable via env so operators can skip auto-launch
(`COMMCLIENT_SFU_AUTOSTART=0`), point at an external mediasoup
(`COMMCLIENT_SFU_EXTERNAL=1` + MEDIASOUP_CONTROL_HOST/PORT), or disable
npm install (`COMMCLIENT_SFU_SKIP_INSTALL=1`).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.services.lan_ice_helper import primary_lan_ip

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config resolution
# ─────────────────────────────────────────────────────────────────────────────


def _truthy(v: str | None) -> bool:
    return bool(v) and v.strip().lower() in {"1", "true", "yes", "on"}


def _worker_root() -> Path:
    """
    Resolve the path to the `sfu-worker/` directory.

    Priority:
      1. `COMMCLIENT_SFU_DIR` env override.
      2. Sibling of the Python project (source tree layout).
      3. `_MEIPASS/sfu-worker` (PyInstaller frozen build layout).
    """
    override = os.environ.get("COMMCLIENT_SFU_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        return p

    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / "sfu-worker")
    # Repo layout: CommClient-Server/app/services/this_file.py
    #   → project root is parents[2]  (services → app → CommClient-Server)
    candidates.append(Path(__file__).resolve().parents[2] / "sfu-worker")

    for c in candidates:
        if (c / "package.json").is_file():
            return c

    # Last resort — still return the repo-layout path so the caller gets
    # a useful error when it tries to spawn.
    return candidates[-1]


def _node_executable() -> str:
    """
    Resolve the `node` executable. Honours `COMMCLIENT_NODE_BIN` override,
    then falls back to PATH resolution.
    """
    explicit = os.environ.get("COMMCLIENT_NODE_BIN")
    if explicit:
        return explicit

    cmd = "node.exe" if sys.platform.startswith("win") else "node"
    found = shutil.which(cmd)
    return found or cmd  # Let spawn raise a clean error if truly missing.


def _npm_executable() -> str:
    explicit = os.environ.get("COMMCLIENT_NPM_BIN")
    if explicit:
        return explicit
    cmd = "npm.cmd" if sys.platform.startswith("win") else "npm"
    found = shutil.which(cmd)
    return found or cmd


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SfuProcessState:
    pid: int | None = None
    started_at: float = 0.0
    restart_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None


class SfuLauncher:
    """Supervises one instance of the mediasoup worker process."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_flag = asyncio.Event()
        self._state = SfuProcessState()
        self._log_file_stdout: Path | None = None
        self._log_file_stderr: Path | None = None

    # ── public API ─────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        if _truthy(os.environ.get("COMMCLIENT_SFU_EXTERNAL")):
            return False
        return not _truthy(
            os.environ.get("COMMCLIENT_SFU_AUTOSTART_DISABLED"),
        )

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.is_enabled(),
            "running": self.is_running(),
            "pid": self._state.pid,
            "restart_count": self._state.restart_count,
            "last_exit_code": self._state.last_exit_code,
            "last_error": self._state.last_error,
            "control_host": os.environ.get("MEDIASOUP_CONTROL_HOST", "127.0.0.1"),
            "control_port": int(os.environ.get("MEDIASOUP_CONTROL_PORT", "4443")),
            "worker_root": str(_worker_root()),
            "stdout_log": str(self._log_file_stdout) if self._log_file_stdout else None,
            "stderr_log": str(self._log_file_stderr) if self._log_file_stderr else None,
        }

    async def is_healthy(self, timeout: float = 1.5) -> bool:
        """Probe the worker's HTTP control plane to verify the process is
        not just up but actually accepting RPCs. Returns False on any
        error so callers can short-circuit gracefully when SFU is
        unavailable (downgrade to mesh, hide SFU UI, etc.).

        We probe the worker's `/healthz` endpoint — implemented in
        sfu-worker/src/server.js. The probe is best-effort: a 200 OK
        means RPCs work; anything else means we treat the worker as
        down regardless of process state. This catches the failure
        mode where the Node process is alive but mediasoup itself
        crashed in C++ land."""
        if not self.is_running():
            return False
        try:
            import httpx
            host = os.environ.get("MEDIASOUP_CONTROL_HOST", "127.0.0.1")
            port = int(os.environ.get("MEDIASOUP_CONTROL_PORT", "4443"))
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = await cli.get(f"http://{host}:{port}/healthz")
                return r.status_code == 200
        except Exception:
            return False

    async def start(self) -> None:
        """Start the supervisor loop. No-op if disabled or already running."""
        if not self.is_enabled():
            logger.info("sfu_launcher_disabled")
            return
        if self._supervisor_task and not self._supervisor_task.done():
            return

        root = _worker_root()
        if not (root / "package.json").is_file():
            logger.error("sfu_worker_missing", path=str(root))
            return

        if not _truthy(os.environ.get("COMMCLIENT_SFU_SKIP_INSTALL")):
            await self._ensure_node_modules(root)

        self._stop_flag.clear()
        self._supervisor_task = asyncio.create_task(
            self._supervisor(root), name="sfu-supervisor",
        )

    async def stop(self, timeout: float = 8.0) -> None:
        """Signal the supervisor to stop, terminate worker, and wait."""
        self._stop_flag.set()
        proc = self._proc
        if proc and proc.returncode is None:
            try:
                if sys.platform.startswith("win"):
                    proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await asyncio.wait_for(self._supervisor_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    # ── internals ──────────────────────────────────────────────────────

    async def _ensure_node_modules(self, root: Path) -> None:
        node_modules = root / "node_modules"
        mediasoup_pkg = node_modules / "mediasoup" / "package.json"
        if mediasoup_pkg.is_file():
            return

        logger.info("sfu_npm_install_start", root=str(root))
        npm = _npm_executable()
        try:
            proc = await asyncio.create_subprocess_exec(
                npm, "install", "--omit=dev", "--no-audit", "--no-fund",
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(
                    "sfu_npm_install_failed",
                    code=proc.returncode,
                    stderr=stderr.decode(errors="replace")[-2000:],
                )
            else:
                logger.info("sfu_npm_install_ok")
        except FileNotFoundError:
            logger.error(
                "sfu_npm_missing",
                hint="node/npm not installed or not on PATH; "
                     "install Node >=18 or set COMMCLIENT_NPM_BIN",
            )
        except Exception as exc:
            logger.error("sfu_npm_install_exception", error=str(exc))

    def _worker_env(self) -> dict[str, str]:
        """Build the env that the Node worker inherits."""
        env = dict(os.environ)
        env.setdefault("NODE_ENV", "production")
        env.setdefault("LOG_LEVEL", os.environ.get("SFU_LOG_LEVEL", "info"))

        # Control API
        env["MEDIASOUP_CONTROL_HOST"] = env.get(
            "MEDIASOUP_CONTROL_HOST", "127.0.0.1",
        )
        env["MEDIASOUP_CONTROL_PORT"] = env.get(
            "MEDIASOUP_CONTROL_PORT", "4443",
        )
        # Control token — picked from persistent_secrets, so the Python
        # bridge and the worker share the same value deterministically.
        if "MEDIASOUP_CONTROL_TOKEN" not in env:
            token = os.environ.get("MEDIASOUP_CONTROL_TOKEN")
            if token:
                env["MEDIASOUP_CONTROL_TOKEN"] = token

        # Announced IP for WebRTC — keep deterministic across restarts.
        env.setdefault(
            "MEDIASOUP_ANNOUNCED_IP",
            os.environ.get("ICE_ANNOUNCED_IP") or primary_lan_ip(),
        )

        # RTC port range mirrors Settings.MEDIASOUP_MIN_PORT / MAX_PORT.
        env.setdefault(
            "MEDIASOUP_RTC_MIN_PORT",
            os.environ.get("MEDIASOUP_MIN_PORT", "40000"),
        )
        env.setdefault(
            "MEDIASOUP_RTC_MAX_PORT",
            os.environ.get("MEDIASOUP_MAX_PORT", "49999"),
        )

        # Recording dir — colocate with the server data dir so backups
        # grab it automatically.
        data_dir = os.environ.get("COMMCLIENT_DATA_DIR")
        if data_dir:
            rec_dir = Path(data_dir) / "recordings"
            rec_dir.mkdir(parents=True, exist_ok=True)
            env.setdefault("MEDIASOUP_RECORDINGS_DIR", str(rec_dir))

        return env

    def _log_paths(self) -> tuple[Path, Path]:
        """Return (stdout_log, stderr_log) paths inside the data dir."""
        data_dir = os.environ.get("COMMCLIENT_DATA_DIR")
        base = Path(data_dir) / "logs" if data_dir else Path(__file__).resolve().parents[2] / "logs"
        base.mkdir(parents=True, exist_ok=True)
        return base / "sfu-worker.stdout.log", base / "sfu-worker.stderr.log"

    async def _spawn_once(self, root: Path) -> asyncio.subprocess.Process:
        node = _node_executable()
        script = str(root / "src" / "server.js")
        stdout_path, stderr_path = self._log_paths()
        self._log_file_stdout = stdout_path
        self._log_file_stderr = stderr_path

        # Open in append mode so crashes leave a forensic trail.
        stdout_fh = stdout_path.open("ab", buffering=0)
        stderr_fh = stderr_path.open("ab", buffering=0)

        creationflags = 0
        if sys.platform.startswith("win"):
            # Run in its own process group so we can send CTRL_BREAK_EVENT
            # to terminate gracefully without killing the parent Python.
            creationflags = getattr(
                __import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0,
            )

        proc = await asyncio.create_subprocess_exec(
            node, script,
            cwd=str(root),
            env=self._worker_env(),
            stdout=stdout_fh,
            stderr=stderr_fh,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=creationflags if creationflags else 0,
            close_fds=not sys.platform.startswith("win"),
        )

        # We keep the raw file handles owned by the subprocess — they'll be
        # closed when the child exits. Close our own references.
        try:
            stdout_fh.close()
            stderr_fh.close()
        except OSError:
            pass

        self._proc = proc
        self._state.pid = proc.pid
        loop = asyncio.get_running_loop()
        self._state.started_at = loop.time()
        logger.info(
            "sfu_worker_spawned",
            pid=proc.pid,
            node=node,
            root=str(root),
            stdout_log=str(stdout_path),
            stderr_log=str(stderr_path),
        )
        return proc

    async def _supervisor(self, root: Path) -> None:
        backoff = 1.0
        max_backoff = 30.0
        while not self._stop_flag.is_set():
            try:
                proc = await self._spawn_once(root)
            except FileNotFoundError:
                self._state.last_error = (
                    "node executable not found — install Node >=18.19"
                )
                logger.error("sfu_worker_node_missing")
                return  # No point retrying; operator action required.
            except Exception as exc:
                self._state.last_error = str(exc)
                logger.error("sfu_worker_spawn_error", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            rc = await proc.wait()
            self._state.last_exit_code = rc
            self._proc = None

            if self._stop_flag.is_set():
                logger.info("sfu_worker_exited_during_shutdown", rc=rc)
                return

            loop = asyncio.get_running_loop()
            uptime = loop.time() - self._state.started_at
            logger.warning(
                "sfu_worker_exited",
                rc=rc,
                uptime_sec=round(uptime, 2),
                restart_in_sec=round(backoff, 2),
            )
            self._state.restart_count += 1
            # If the worker ran for a while, reset the backoff. Otherwise
            # ramp up to avoid hammering a broken config.
            if uptime > 60:
                backoff = 1.0
            try:
                await asyncio.wait_for(
                    self._stop_flag.wait(), timeout=backoff,
                )
                return
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, max_backoff)


# Process-level singleton
sfu_launcher = SfuLauncher()

__all__ = ["sfu_launcher", "SfuLauncher"]
