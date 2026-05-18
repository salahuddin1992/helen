"""Local NAT-type detector — uses STUN to classify our binding behaviour.

Classification heuristic (no full RFC 3489 test suite — that test
needs two external IPs; we keep it simple for LAN-first deployments):

  * No STUN server configured            → UNKNOWN
  * STUN succeeds, ext_ip == local_ip    → OPEN
  * STUN succeeds, ext_ip != local_ip    → PORT_RESTRICTED (default
                                           guess — most home routers).
  * STUN fails                           → UNKNOWN

For higher fidelity, set ``HELEN_NAT_STUN_SECONDARY=host:port`` to
enable the full RFC 3489 Test II + Test III sequence (not implemented
in v1 — left as an extension point).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Optional

from app.core.logging import get_logger
from app.nat.nat_config import get_config
from app.nat.nat_events import emit
from app.nat.nat_type import NATType
from app.nat import stun_client

logger = get_logger(__name__)


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


class NATDetector:
    _singleton: "NATDetector | None" = None

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._type: NATType = NATType.UNKNOWN
        self._public_ip: str = ""
        self._public_port: int = 0
        self._local_ip: str = _local_ip()
        self._last_check_at: float = 0.0
        self._loop_task: Optional[asyncio.Task] = None
        self._running = False

    @classmethod
    def instance(cls) -> "NATDetector":
        if cls._singleton is None:
            cls._singleton = NATDetector()
        return cls._singleton

    # ── Detection ─────────────────────────────────────────

    async def detect_once(self) -> NATType:
        cfg = get_config()
        if not cfg.stun_server:
            with self._lock:
                self._type = NATType.UNKNOWN
                self._last_check_at = time.time()
            return NATType.UNKNOWN
        try:
            host, port = await stun_client.query(
                cfg.stun_server, cfg.stun_port,
                timeout=cfg.detect_timeout_sec,
            )
        except Exception as e:
            logger.warning("nat_detect_stun_failed", error=str(e))
            with self._lock:
                self._type = NATType.UNKNOWN
                self._last_check_at = time.time()
            return NATType.UNKNOWN

        new_type = (NATType.OPEN if host == self._local_ip
                    else NATType.PORT_RESTRICTED)
        with self._lock:
            old = self._type
            self._type = new_type
            self._public_ip = host
            self._public_port = port
            self._last_check_at = time.time()
        if old is not new_type:
            emit("nat.type_changed", {
                "old": old.value, "new": new_type.value,
                "public_ip": host, "public_port": port,
            })
        return new_type

    def current(self) -> NATType:
        with self._lock:
            return self._type

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "type":          self._type.value,
                "public_ip":     self._public_ip,
                "public_port":   self._public_port,
                "local_ip":      self._local_ip,
                "last_check_at": self._last_check_at,
                "stun_configured": bool(get_config().stun_server),
            }

    # ── Background loop ───────────────────────────────────

    async def _run_loop(self) -> None:
        cfg = get_config()
        self._running = True
        logger.info("nat_detector_started",
                    interval_sec=cfg.redetect_interval_sec)
        try:
            while self._running:
                try:
                    await self.detect_once()
                except Exception as e:
                    logger.warning("nat_detect_failed", error=str(e))
                await asyncio.sleep(cfg.redetect_interval_sec)
        finally:
            logger.info("nat_detector_stopped")

    def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
            self._loop_task = loop.create_task(
                self._run_loop(), name="nat-detector",
            )
        except RuntimeError:
            logger.warning("nat_detector_no_event_loop_yet")

    def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None


def get_nat_detector() -> NATDetector:
    return NATDetector.instance()
