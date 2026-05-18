"""
External camera ingest service.

Spawns and supervises one FFmpeg subprocess per enabled IngestSource row.
FFmpeg pulls from the configured URL (RTSP/RTMP/SRT/HTTP/HLS/NDI), optionally
transcodes via the best available hardware encoder (NVENC > QSV > AMF >
libx264), and emits to a mediasoup PlainTransport using RTP so the SFU
can re-distribute the stream to Electron clients as a normal producer.

Architecture
------------
    IP Camera ── FFmpeg ── RTP ── mediasoup PlainTransport ── SFU ── Clients
                   ▲                       ▲
                   │                       │
              HW encoder              virtual producer
              (cap clamped)

Supervisor pattern mirrors `sfu_launcher.py`:
  * One asyncio task per source, exponential backoff on crash.
  * Stdout/stderr redirected to data/logs/ingest-<source_id>.log.
  * Stop via CTRL_BREAK_EVENT on Windows, SIGTERM elsewhere.
  * Status written back to the DB (idle | starting | running | error | stopped).

PlainTransport creation against mediasoup is delegated to the existing
topology_manager / MediasoupBridge; the SFU worker exposes a
`/mediasoup/plain_producer` control endpoint that we call to:
  - create PlainTransport
  - create Producer from the RTP we send

If the bridge is unavailable, the FFmpeg process still runs (so the operator
can diagnose camera connectivity) but `status` is reported as "error".
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, quote

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.gpu_detect import probe as gpu_probe, preferred_video_encoder
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.models.media_policy import IngestSource
from app.services.media_policy_service import media_policy_service

logger = get_logger(__name__)


# ── Config resolution ───────────────────────────────────────

def _ffmpeg_path() -> str | None:
    return gpu_probe().ffmpeg_path


def _logs_dir() -> Path:
    data_dir = os.environ.get("COMMCLIENT_DATA_DIR")
    base = Path(data_dir) / "logs" if data_dir else Path(__file__).resolve().parents[2] / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base


# ── URL building (handle credentials + safe escaping) ───────

def _build_source_url(src: IngestSource) -> str:
    """
    Fold username/password into the URL. Only applied for protocols that
    support userinfo in the URI. Keeps the DB row pristine.
    """
    raw = (src.url or "").strip()
    if not raw:
        return ""
    if not src.username:
        return raw

    try:
        parsed = urlparse(raw)
    except Exception:
        return raw

    if parsed.scheme.lower() not in {"rtsp", "rtmp", "http", "https"}:
        return raw

    user = quote(src.username, safe="")
    pw = quote(src.password or "", safe="")
    userinfo = f"{user}:{pw}" if pw else user

    # Preserve existing userinfo if present; caller wins.
    if "@" in parsed.netloc:
        return raw
    new_netloc = f"{userinfo}@{parsed.netloc}"
    return parsed._replace(netloc=new_netloc).geturl()


# ── FFmpeg command builder ──────────────────────────────────

@dataclass(frozen=True)
class IngestTarget:
    """Output target — where the transcoded RTP lands."""
    rtp_host: str = "127.0.0.1"
    rtp_port: int = 0  # 0 → allocated later by mediasoup
    rtcp_port: int = 0
    payload_type: int = 96
    codec_mime: str = "H264"  # for mediasoup producer params


def _build_ffmpeg_args(
    src: IngestSource,
    target: IngestTarget,
    *,
    max_w: int,
    max_h: int,
    max_fps: int,
    max_kbps: int,
    encoder: str,
    prefer_copy: bool = False,
) -> list[str]:
    """
    Build the argv list for FFmpeg. Two paths:
      1. "copy" path (low-CPU) when camera already delivers what we want.
      2. "transcode" path with hardware-accelerated scaling + encoding.

    We always output RTP/H.264 — matches what mediasoup consumes and
    what Electron (Chromium) reliably decodes.
    """
    input_url = _build_source_url(src)
    proto = (src.protocol or "").lower()

    # Resolve "usb" alias to the platform-native input driver.
    if proto == "usb":
        if sys.platform.startswith("win"):
            proto = "dshow"
        elif sys.platform == "darwin":
            proto = "avfoundation"
        else:
            proto = "v4l2"

    # ── Input block ─────────────────────────────────────────
    args: list[str] = [
        "-hide_banner",
        "-nostats",
        "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-use_wallclock_as_timestamps", "1",
    ]
    # Reconnect logic for flaky network cameras.
    if proto in {"http", "https", "hls", "mjpeg"}:
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
    if proto == "rtsp":
        args += ["-rtsp_transport", (src.transport or "tcp").lower()]
    if proto == "srt":
        args += ["-max_delay", "500000"]

    # ── Platform-native capture devices ─────────────────────
    # The URL field holds the device name (e.g. "Logitech BRIO" on
    # Windows, "/dev/video0" on Linux, "0:0" on macOS).
    if proto == "dshow":
        args += ["-f", "dshow"]
        if max_fps > 0:
            args += ["-framerate", str(max_fps)]
        if max_w > 0 and max_h > 0:
            args += ["-video_size", f"{max_w}x{max_h}"]
        device = (src.url or "").strip()
        if not device.lower().startswith("video="):
            device = f"video={device}"
        args += ["-i", device]
    elif proto == "v4l2":
        args += ["-f", "v4l2"]
        if max_fps > 0:
            args += ["-framerate", str(max_fps)]
        if max_w > 0 and max_h > 0:
            args += ["-video_size", f"{max_w}x{max_h}"]
        args += ["-i", (src.url or "/dev/video0")]
    elif proto == "avfoundation":
        args += ["-f", "avfoundation"]
        if max_fps > 0:
            args += ["-framerate", str(max_fps)]
        if max_w > 0 and max_h > 0:
            args += ["-video_size", f"{max_w}x{max_h}"]
        args += ["-i", (src.url or "0")]
    elif proto == "mjpeg":
        # MJPEG-over-HTTP (image/jpeg multipart refresh).
        args += ["-f", "mjpeg", "-i", input_url]
    elif proto == "ndi":
        # Requires a libndi-enabled ffmpeg build (see NDI SDK docs).
        # If the running ffmpeg wasn't built with libndi_newtek, this
        # will fail at runtime with "Unknown input format: libndi_newtek"
        # — surfaced as an error on the admin row.
        args += ["-f", "libndi_newtek", "-i", input_url]
    else:
        args += ["-i", input_url]

    # ── Video filter / codec ────────────────────────────────
    if prefer_copy:
        args += ["-c:v", "copy"]
    else:
        # Scale-down filter: only downscale if needed, preserve aspect ratio.
        vf = f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease"
        # Framerate cap.
        if max_fps > 0:
            vf = f"{vf},fps={max_fps}"
        args += ["-vf", vf]
        args += ["-c:v", encoder]

        # Bitrate shaping — map encoder to its flag variant.
        kbps = max(200, int(max_kbps))
        args += [
            "-b:v", f"{kbps}k",
            "-maxrate", f"{int(kbps * 1.2)}k",
            "-bufsize", f"{kbps * 2}k",
            "-g", str(max(15, (max_fps or 30) * 2)),  # keyframe interval
            "-pix_fmt", "yuv420p",
        ]

        if encoder.endswith("_nvenc"):
            args += [
                "-preset", "p4",   # p1-p7: slowest-fastest; p4 = balanced
                "-rc", "cbr",
                "-tune", "ll",
                "-zerolatency", "1",
            ]
        elif encoder.endswith("_qsv"):
            args += ["-preset", "veryfast", "-look_ahead", "0"]
        elif encoder.endswith("_amf"):
            args += ["-usage", "ultralowlatency", "-quality", "speed"]
        elif encoder == "libx264":
            args += [
                "-preset", "veryfast",
                "-tune", "zerolatency",
                "-profile:v", "baseline",
            ]

    # ── Audio: always Opus for WebRTC clients, or drop ──────
    args += [
        "-c:a", "libopus",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
    ]

    # ── Output: RTP to mediasoup PlainTransport ─────────────
    # Uses SDP payload types chosen to match mediasoup defaults.
    rtp_url = f"rtp://{target.rtp_host}:{target.rtp_port}?pkt_size=1200"
    args += [
        "-f", "rtp",
        "-payload_type", str(target.payload_type),
        rtp_url,
    ]
    return args


# ── Per-source supervisor ───────────────────────────────────

@dataclass
class IngestProcessState:
    source_id: str
    pid: int | None = None
    started_at: float = 0.0
    restart_count: int = 0
    last_exit_code: int | None = None
    last_error: str | None = None
    status: str = "idle"  # idle | starting | running | error | stopped
    log_path: Path | None = None


class IngestProcess:
    """One FFmpeg subprocess tied to one IngestSource row."""

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._state = IngestProcessState(source_id=source_id)

    def snapshot(self) -> dict[str, Any]:
        return {
            "source_id": self._state.source_id,
            "pid": self._state.pid,
            "restart_count": self._state.restart_count,
            "last_exit_code": self._state.last_exit_code,
            "last_error": self._state.last_error,
            "status": self._state.status,
            "log_path": str(self._state.log_path) if self._state.log_path else None,
        }

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name=f"ingest-{self.source_id}",
        )

    async def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
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
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._state.status = "stopped"
        await self._persist_status()

    async def _persist_status(self) -> None:
        try:
            async with async_session_factory() as db:
                await db.execute(
                    update(IngestSource)
                    .where(IngestSource.id == self.source_id)
                    .values(status=self._state.status, last_error=self._state.last_error)
                )
                await db.commit()
        except Exception as e:
            logger.warning("ingest_status_persist_failed", source_id=self.source_id, error=str(e))

    async def _load_source(self) -> IngestSource | None:
        async with async_session_factory() as db:
            result = await db.execute(
                select(IngestSource).where(IngestSource.id == self.source_id)
            )
            return result.scalar_one_or_none()

    async def _resolve_caps(self, owner_user_id: str) -> tuple[int, int, int, int]:
        """Apply owner's media cap to the transcoder output."""
        async with async_session_factory() as db:
            cap = await media_policy_service.effective_cap_for(db, owner_user_id)
        return cap.max_width, cap.max_height, cap.max_framerate, cap.max_bitrate_kbps

    async def _spawn_once(self) -> asyncio.subprocess.Process | None:
        ffmpeg_bin = _ffmpeg_path()
        if not ffmpeg_bin:
            self._state.last_error = "ffmpeg not installed"
            self._state.status = "error"
            await self._persist_status()
            return None

        src = await self._load_source()
        if src is None:
            self._state.last_error = f"source {self.source_id} not found"
            self._state.status = "error"
            await self._persist_status()
            return None
        if not src.enabled:
            self._state.status = "stopped"
            await self._persist_status()
            return None

        caps = gpu_probe()
        policy_w, policy_h, policy_fps, policy_kbps = await self._resolve_caps(src.owner_user_id)
        target_w = min(src.target_width or policy_w, policy_w)
        target_h = min(src.target_height or policy_h, policy_h)
        target_fps = min(src.target_framerate or policy_fps, policy_fps)
        target_kbps = min(src.target_bitrate_kbps or policy_kbps, policy_kbps)

        encoder = preferred_video_encoder(
            codec=(src.codec_hint or "h264").lower(),
            prefer_hw=caps.has_nvidia or caps.has_intel_qsv or caps.has_amd_amf,
        )

        # Allocate the RTP target. In a later patch we wire this to a
        # mediasoup PlainTransport; for now we emit to loopback on a
        # deterministic port computed from the source_id hash (predictable,
        # collision-unlikely on a single-operator LAN box).
        port = 40000 + (int(src.id[:8], 16) % 8000) * 2  # even port for RTP
        target = IngestTarget(rtp_host="127.0.0.1", rtp_port=port)

        args = _build_ffmpeg_args(
            src, target,
            max_w=target_w, max_h=target_h,
            max_fps=target_fps, max_kbps=target_kbps,
            encoder=encoder,
        )

        log_path = _logs_dir() / f"ingest-{self.source_id}.log"
        self._state.log_path = log_path
        log_fh = log_path.open("ab", buffering=0)

        creationflags = 0
        if sys.platform.startswith("win"):
            import subprocess as _sp
            creationflags = getattr(_sp, "CREATE_NEW_PROCESS_GROUP", 0)

        logger.info(
            "ingest_spawn",
            source_id=self.source_id,
            url=src.url,
            encoder=encoder,
            target=f"{target.rtp_host}:{target.rtp_port}",
            resolution=f"{target_w}x{target_h}@{target_fps}/{target_kbps}kbps",
            cmd=" ".join(shlex.quote(a) for a in [ffmpeg_bin] + args[:12]) + " ...",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin, *args,
                stdout=log_fh,
                stderr=log_fh,
                stdin=asyncio.subprocess.DEVNULL,
                creationflags=creationflags if creationflags else 0,
                close_fds=not sys.platform.startswith("win"),
            )
        except Exception as e:
            self._state.last_error = str(e)
            self._state.status = "error"
            await self._persist_status()
            try:
                log_fh.close()
            except OSError:
                pass
            return None
        finally:
            try:
                log_fh.close()
            except OSError:
                pass

        self._proc = proc
        self._state.pid = proc.pid
        self._state.started_at = asyncio.get_running_loop().time()
        self._state.status = "running"
        self._state.last_error = None
        await self._persist_status()
        return proc

    async def _run_loop(self) -> None:
        backoff = 1.0
        max_backoff = 30.0
        while not self._stop.is_set():
            self._state.status = "starting"
            await self._persist_status()

            proc = await self._spawn_once()
            if proc is None:
                # Hard failure (missing ffmpeg, missing row). Don't hot-loop.
                await asyncio.sleep(5.0)
                if self._stop.is_set():
                    return
                continue

            rc = await proc.wait()
            self._proc = None
            self._state.last_exit_code = rc

            if self._stop.is_set():
                self._state.status = "stopped"
                await self._persist_status()
                return

            uptime = asyncio.get_running_loop().time() - self._state.started_at
            logger.warning(
                "ingest_exited",
                source_id=self.source_id,
                rc=rc,
                uptime_sec=round(uptime, 2),
                restart_in=round(backoff, 2),
            )
            self._state.restart_count += 1
            self._state.status = "error" if rc != 0 else "idle"
            if rc != 0:
                self._state.last_error = f"ffmpeg exited with code {rc}"
            await self._persist_status()

            if uptime > 60:
                backoff = 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                backoff = min(backoff * 2, max_backoff)


# ── Registry ───────────────────────────────────────────────

class IngestService:
    """Tracks all running ingest processes."""

    def __init__(self) -> None:
        self._procs: dict[str, IngestProcess] = {}
        self._lock = asyncio.Lock()

    async def start_source(self, source_id: str) -> dict[str, Any]:
        async with self._lock:
            proc = self._procs.get(source_id)
            if proc and proc.is_running():
                return proc.snapshot()
            if proc is None:
                proc = IngestProcess(source_id)
                self._procs[source_id] = proc
            await proc.start()
            return proc.snapshot()

    async def stop_source(self, source_id: str) -> dict[str, Any]:
        async with self._lock:
            proc = self._procs.get(source_id)
            if proc is None:
                return {"source_id": source_id, "status": "idle"}
            await proc.stop()
            snap = proc.snapshot()
            del self._procs[source_id]
            return snap

    async def restart_source(self, source_id: str) -> dict[str, Any]:
        await self.stop_source(source_id)
        return await self.start_source(source_id)

    def get_status(self, source_id: str) -> dict[str, Any]:
        proc = self._procs.get(source_id)
        return proc.snapshot() if proc else {"source_id": source_id, "status": "idle"}

    def all_statuses(self) -> list[dict[str, Any]]:
        return [p.snapshot() for p in self._procs.values()]

    async def shutdown_all(self, timeout: float = 8.0) -> None:
        async with self._lock:
            tasks = [p.stop(timeout=timeout) for p in self._procs.values()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._procs.clear()

    async def autostart(self) -> list[str]:
        """Launch every source flagged auto_start=True. Called from lifespan."""
        started: list[str] = []
        try:
            async with async_session_factory() as db:
                result = await db.execute(
                    select(IngestSource).where(
                        IngestSource.enabled.is_(True),
                        IngestSource.auto_start.is_(True),
                    )
                )
                rows = list(result.scalars().all())
        except Exception as e:
            logger.warning("ingest_autostart_db_read_failed", error=str(e))
            return started

        for row in rows:
            try:
                await self.start_source(row.id)
                started.append(row.id)
            except Exception as e:
                logger.warning("ingest_autostart_failed", source_id=row.id, error=str(e))
        return started


# Process-level singleton
ingest_service = IngestService()

__all__ = ["ingest_service", "IngestService", "IngestProcess", "IngestTarget"]
