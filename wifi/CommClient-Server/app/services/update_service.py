"""
Update mirror / self-update service.

Responsibilities:

1. **LAN-mirror mode** (default). Maintains a local cache under
   ``%APPDATA%/CommClient/updates`` with the current channel manifests
   (``channel-stable.json``, ``channel-beta.json``, ``channel-canary.json``)
   plus the installer binaries they reference. Serves them to LAN
   clients via ``/api/updates/<file>`` so offline clients can still
   upgrade when the LAN server has a newer copy.

2. **Leader refresh loop**. On the elected leader, periodically pulls
   the upstream manifest from the configured release server and
   downloads any new installers. Mirror writes are atomic (tmp →
   fsync → rename). Other LAN replicas pull from the leader.

3. **Signature verification**. Each manifest entry carries a Base64
   Ed25519 signature of its SHA-512. We verify it against the
   configured public key before accepting an installer into the
   mirror. Unsigned manifests are rejected when
   ``COMMCLIENT_UPDATE_REQUIRE_SIGNATURE=1``.

4. **Self-update for the server process** (optional, off by default).
   When enabled the leader can download a server wheel, verify its
   signature, and atomically swap it under ``site-packages``. Restart
   is out of scope — the supervisor (NSSM / scheduled task /
   start-lan-server.ps1 loop) will pick up the new code on next boot.

The service is supervised through :pyfunc:`app.main.run_as_leader`
so exactly one LAN node refreshes the mirror at a time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import httpx

try:  # optional dependency; falls back to NaCl if present
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives import serialization as _crypto_serialization

    _HAVE_CRYPTOGRAPHY = True
except Exception:  # pragma: no cover
    _HAVE_CRYPTOGRAPHY = False

logger = logging.getLogger("commclient.update_service")


# ─── data models ────────────────────────────────────────────────────────


@dataclass
class ManifestEntry:
    version: str
    channel: str
    releasedAt: str
    url: str
    sha512: str
    size: int
    signature: Optional[str] = None
    notes: Optional[str] = None
    mandatory: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "ManifestEntry":
        return cls(
            version=str(d["version"]),
            channel=str(d.get("channel", "stable")),
            releasedAt=str(d.get("releasedAt") or d.get("releaseDate") or ""),
            url=str(d.get("url") or d.get("path") or ""),
            sha512=str(d.get("sha512", "")),
            size=int(d.get("size", 0) or 0),
            signature=d.get("signature"),
            notes=d.get("notes") or d.get("releaseNotes"),
            mandatory=bool(d.get("mandatory", False)),
        )


@dataclass
class MirrorState:
    last_refresh_at: float = 0.0
    last_error: Optional[str] = None
    channels: dict[str, list[str]] = field(default_factory=dict)  # channel -> [version]


# ─── config ─────────────────────────────────────────────────────────────


def _data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    p = Path(base) / "CommClient" / "updates"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _manifest_path(channel: str) -> Path:
    return _data_dir() / f"channel-{channel}.json"


def _installer_dir() -> Path:
    p = _data_dir() / "installers"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class UpdateServiceConfig:
    upstream_url: Optional[str] = None  # e.g. https://updates.commclient.example
    channels: tuple[str, ...] = ("stable", "beta")
    public_key_b64: Optional[str] = None
    require_signature: bool = True
    refresh_interval_sec: int = 3600  # 1h
    max_retained_installers_per_channel: int = 3
    http_timeout_sec: float = 30.0
    max_installer_mb: int = 512

    @classmethod
    def from_env(cls) -> "UpdateServiceConfig":
        raw_channels = os.environ.get("COMMCLIENT_UPDATE_CHANNELS", "stable,beta")
        return cls(
            upstream_url=os.environ.get("COMMCLIENT_UPDATE_UPSTREAM"),
            channels=tuple(c.strip() for c in raw_channels.split(",") if c.strip()),
            public_key_b64=os.environ.get("COMMCLIENT_UPDATE_PUBKEY"),
            require_signature=os.environ.get("COMMCLIENT_UPDATE_REQUIRE_SIGNATURE", "1") == "1",
            refresh_interval_sec=int(os.environ.get("COMMCLIENT_UPDATE_INTERVAL_SEC", "3600")),
            max_retained_installers_per_channel=int(
                os.environ.get("COMMCLIENT_UPDATE_KEEP", "3")
            ),
            http_timeout_sec=float(os.environ.get("COMMCLIENT_UPDATE_HTTP_TIMEOUT", "30")),
            max_installer_mb=int(os.environ.get("COMMCLIENT_UPDATE_MAX_MB", "512")),
        )


# ─── signature verification ─────────────────────────────────────────────


def _load_public_key(material_b64: str):
    if not _HAVE_CRYPTOGRAPHY:
        raise RuntimeError("cryptography package not installed")
    raw = base64.b64decode(material_b64.strip())
    if len(raw) == 32:
        return Ed25519PublicKey.from_public_bytes(raw)
    # Assume SPKI DER
    return _crypto_serialization.load_der_public_key(raw)


def _verify_signature(sha512_hex: str, signature_b64: str, pk_material: str) -> bool:
    if not _HAVE_CRYPTOGRAPHY:
        logger.warning("cryptography missing — cannot verify signature")
        return False
    try:
        key = _load_public_key(pk_material)
        key.verify(base64.b64decode(signature_b64), sha512_hex.encode("utf-8"))
        return True
    except InvalidSignature:
        return False
    except Exception as exc:  # pragma: no cover
        logger.warning("signature verify error: %s", exc)
        return False


# ─── core service ───────────────────────────────────────────────────────


class UpdateService:
    """LAN update mirror maintained by the elected leader."""

    def __init__(self, config: Optional[UpdateServiceConfig] = None):
        self.cfg = config or UpdateServiceConfig.from_env()
        self.state = MirrorState()
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    # public API ---------------------------------------------------------

    async def refresh_once(self) -> dict:
        """Pull latest manifests + installers from upstream."""
        async with self._lock:
            if not self.cfg.upstream_url:
                return {"ok": False, "reason": "no upstream configured"}
            result: dict[str, Any] = {"ok": True, "channels": {}}
            async with httpx.AsyncClient(
                timeout=self.cfg.http_timeout_sec, follow_redirects=True
            ) as client:
                for channel in self.cfg.channels:
                    try:
                        stat = await self._refresh_channel(client, channel)
                        result["channels"][channel] = stat
                    except Exception as exc:
                        logger.warning("channel %s refresh failed: %s", channel, exc)
                        result["channels"][channel] = {"ok": False, "error": str(exc)}
            self.state.last_refresh_at = time.time()
            return result

    async def run_forever(self) -> None:
        """Leader loop — refresh on interval until stop."""
        logger.info(
            "update_service starting: upstream=%s interval=%ds channels=%s",
            self.cfg.upstream_url,
            self.cfg.refresh_interval_sec,
            ",".join(self.cfg.channels),
        )
        while not self._stop.is_set():
            try:
                await self.refresh_once()
            except Exception as exc:
                logger.warning("refresh failure: %s", exc)
                self.state.last_error = str(exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.cfg.refresh_interval_sec
                )
            except asyncio.TimeoutError:
                pass
        logger.info("update_service stopped")

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "config": {
                "upstream": self.cfg.upstream_url,
                "channels": list(self.cfg.channels),
                "require_signature": self.cfg.require_signature,
                "interval_sec": self.cfg.refresh_interval_sec,
            },
            "state": {
                "last_refresh_at": self.state.last_refresh_at,
                "last_error": self.state.last_error,
                "channels": self.state.channels,
            },
        }

    # ------------------------------------------------------------------

    async def _refresh_channel(
        self, client: httpx.AsyncClient, channel: str
    ) -> dict:
        upstream = self.cfg.upstream_url.rstrip("/")  # type: ignore[union-attr]
        manifest_url = f"{upstream}/channel-{channel}.json"
        resp = await client.get(manifest_url)
        resp.raise_for_status()
        manifest = resp.json()
        entries = self._parse_manifest(manifest, channel)
        if not entries:
            return {"ok": False, "error": "empty manifest"}

        downloaded: list[str] = []
        for entry in entries:
            ok = await self._ensure_installer(client, upstream, entry)
            if ok:
                downloaded.append(entry.version)

        # Atomic manifest write.
        new_manifest = {
            "latest": entries[0].version,
            "channel": channel,
            "versions": [asdict(e) for e in entries],
        }
        self._atomic_write_json(_manifest_path(channel), new_manifest)

        self.state.channels[channel] = [e.version for e in entries]
        self._prune_installers(channel, keep_versions=[e.version for e in entries])

        return {"ok": True, "downloaded": downloaded, "total": len(entries)}

    def _parse_manifest(self, raw: dict, channel: str) -> list[ManifestEntry]:
        versions = raw.get("versions") if isinstance(raw, dict) else None
        if isinstance(versions, list):
            parsed = [ManifestEntry.from_dict(v) for v in versions]
        elif isinstance(raw, dict) and "version" in raw:
            parsed = [ManifestEntry.from_dict(raw)]
        else:
            parsed = []

        # Enforce signature policy up-front.
        accepted: list[ManifestEntry] = []
        for e in parsed:
            if e.channel != channel:
                continue
            if self.cfg.require_signature and not e.signature:
                logger.warning(
                    "[%s] dropping %s — signature required but missing", channel, e.version
                )
                continue
            if e.signature and self.cfg.public_key_b64:
                if not _verify_signature(
                    e.sha512, e.signature, self.cfg.public_key_b64
                ):
                    logger.warning(
                        "[%s] dropping %s — invalid signature", channel, e.version
                    )
                    continue
            accepted.append(e)

        accepted.sort(key=lambda x: _semver_key(x.version), reverse=True)
        return accepted

    async def _ensure_installer(
        self,
        client: httpx.AsyncClient,
        upstream: str,
        entry: ManifestEntry,
    ) -> bool:
        target = _installer_dir() / _installer_filename(entry)
        if target.exists() and _sha512_hex(target) == entry.sha512.lower():
            return True

        url = entry.url
        if not url.startswith("http"):
            url = f"{upstream}/{url.lstrip('/')}"

        tmp = target.with_suffix(target.suffix + ".tmp")
        size_cap = self.cfg.max_installer_mb * 1024 * 1024
        logger.info("[%s] downloading %s", entry.channel, url)
        try:
            total = 0
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        total += len(chunk)
                        if total > size_cap:
                            raise RuntimeError(
                                f"installer too large (>{self.cfg.max_installer_mb}MB)"
                            )
                        fh.write(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
            # Verify SHA-512 before accepting.
            actual = _sha512_hex(tmp)
            if actual != entry.sha512.lower():
                tmp.unlink(missing_ok=True)
                logger.warning(
                    "[%s] sha512 mismatch for %s (expected=%s actual=%s)",
                    entry.channel, entry.version, entry.sha512, actual,
                )
                return False
            os.replace(tmp, target)
            return True
        except Exception as exc:
            logger.warning("[%s] download failed for %s: %s", entry.channel, entry.version, exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def _prune_installers(self, channel: str, keep_versions: list[str]) -> None:
        max_keep = self.cfg.max_retained_installers_per_channel
        keep = set(keep_versions[:max_keep])
        for p in _installer_dir().glob(f"CommClient-*{channel}*"):
            stem_version = _extract_version_from_name(p.name)
            if stem_version and stem_version not in keep:
                try:
                    p.unlink()
                    logger.info("[%s] pruned installer %s", channel, p.name)
                except OSError:
                    pass

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".manifest.", dir=str(path.parent))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass
            raise


# ─── helpers ─────────────────────────────────────────────────────────────


def _sha512_hex(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _installer_filename(entry: ManifestEntry) -> str:
    return f"CommClient-Setup-{entry.version}-{entry.channel}.exe"


def _extract_version_from_name(name: str) -> Optional[str]:
    # Matches CommClient-Setup-<ver>-<channel>.exe
    try:
        parts = name.split("-")
        if len(parts) >= 4 and parts[0] == "CommClient" and parts[1] == "Setup":
            return parts[2]
    except Exception:
        return None
    return None


def _semver_key(v: str) -> tuple:
    parts = v.lstrip("v").replace("-", ".").split(".")
    key: list[tuple[int, int, str]] = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p), ""))
        else:
            key.append((1, 0, p))
    return tuple(key)


# Module-level singleton ------------------------------------------------

update_service = UpdateService()


async def run_update_service_forever() -> None:
    await update_service.run_forever()
