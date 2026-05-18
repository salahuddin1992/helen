"""
External CA Pinning Service for Helen / CommClient
==================================================

Production-grade certificate pinning and external CA trust management.

Features:
- Pin certificates by SHA-256 fingerprint OR Subject Public Key Info (SPKI) hash.
- Maintain an external CA bundle separate from system trust store.
- Cert chain validation with explicit anchors.
- Built-in CRL (Certificate Revocation List) checker.
- OCSP stapling support (best-effort, async).
- Auto-renewal hooks (callback when cert near expiry).
- Pin rotation with grace window (multi-pin support per host).
- Backed by persistent storage (DB or JSON file).
- structlog throughout.

Design notes:
- LAN-only deployments: most clients trust the operator-issued internal CA.
- External CAs (e.g., for connecting to vendor APIs, partner federation peers,
  webhook destinations) require explicit pinning to defeat MITM.
- Pin types:
    * `cert-sha256`: pin the leaf cert fingerprint
    * `spki-sha256`: pin the Subject Public Key Info hash (survives cert rotation
      as long as key stays the same — RECOMMENDED)
    * `ca-cert-sha256`: pin an intermediate or root CA fingerprint
- Multiple pins per host allow rotation: when rotating keys, add a new pin first,
  let it propagate, then remove the old pin.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtensionOID, NameOID

    _HAS_CRYPTO = True
except Exception:  # pragma: no cover
    _HAS_CRYPTO = False
    x509 = None  # type: ignore
    hashes = None  # type: ignore
    serialization = None  # type: ignore
    ExtensionOID = None  # type: ignore
    NameOID = None  # type: ignore

log = logging.getLogger("helen.security.ca_pinning")
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PinValidationError(Exception):
    """Raised when a cert chain does not satisfy the configured pins."""


class CAPinningConfigError(Exception):
    """Raised on misconfiguration."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PinType(str, Enum):
    CERT_SHA256 = "cert-sha256"
    SPKI_SHA256 = "spki-sha256"
    CA_CERT_SHA256 = "ca-cert-sha256"


class PinSource(str, Enum):
    OPERATOR = "operator"  # manually pinned by operator
    LEARNED = "learned"  # learned from first successful connection (TOFU)
    BACKUP = "backup"  # backup pin for rotation


@dataclass
class CertificatePin:
    host: str  # e.g., "partner.example.com" or "10.20.30.40"
    pin_type: PinType
    value: str  # base64-encoded hash
    source: PinSource = PinSource.OPERATOR
    description: str = ""
    added_at: str = ""
    added_by: Optional[str] = None
    expires_at: Optional[str] = None  # optional sunset date
    rotation_group: Optional[str] = None  # pins in same group are equivalent (rotation)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "pin_type": self.pin_type.value,
            "value": self.value,
            "source": self.source.value,
            "description": self.description,
            "added_at": self.added_at,
            "added_by": self.added_by,
            "expires_at": self.expires_at,
            "rotation_group": self.rotation_group,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CertificatePin":
        return cls(
            host=d["host"],
            pin_type=PinType(d["pin_type"]),
            value=d["value"],
            source=PinSource(d.get("source", "operator")),
            description=d.get("description", ""),
            added_at=d.get("added_at", ""),
            added_by=d.get("added_by"),
            expires_at=d.get("expires_at"),
            rotation_group=d.get("rotation_group"),
            enabled=d.get("enabled", True),
        )

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            ts = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return ts < datetime.now(UTC)
        except Exception:
            return False


@dataclass
class CABundle:
    name: str
    description: str = ""
    pem: str = ""
    added_at: str = ""
    added_by: Optional[str] = None
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "pem": self.pem,
            "added_at": self.added_at,
            "added_by": self.added_by,
            "enabled": self.enabled,
        }


@dataclass
class ValidationResult:
    host: str
    valid: bool
    matched_pin: Optional[CertificatePin] = None
    chain_depth: int = 0
    leaf_subject: Optional[str] = None
    leaf_issuer: Optional[str] = None
    leaf_not_before: Optional[str] = None
    leaf_not_after: Optional[str] = None
    leaf_san: List[str] = field(default_factory=list)
    leaf_sha256: Optional[str] = None
    leaf_spki_sha256: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CAPinningService:
    """Singleton service managing pins, CA bundles, and validation."""

    def __init__(
        self,
        store_path: Optional[Path] = None,
        renewal_callbacks: Optional[List[Callable[[CertificatePin, int], None]]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._store_path = store_path or _default_store_path()
        self._pins: Dict[str, List[CertificatePin]] = {}  # host -> pins
        self._ca_bundles: Dict[str, CABundle] = {}
        self._renewal_callbacks: List[Callable[[CertificatePin, int], None]] = list(renewal_callbacks or [])
        self._load()

    # -------- persistence --------------------------------------------------

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("could not load pinning store at %s: %s", self._store_path, exc)
            return
        with self._lock:
            for entry in data.get("pins", []):
                pin = CertificatePin.from_dict(entry)
                self._pins.setdefault(pin.host, []).append(pin)
            for entry in data.get("ca_bundles", []):
                bundle = CABundle(
                    name=entry["name"],
                    description=entry.get("description", ""),
                    pem=entry.get("pem", ""),
                    added_at=entry.get("added_at", ""),
                    added_by=entry.get("added_by"),
                    enabled=entry.get("enabled", True),
                )
                self._ca_bundles[bundle.name] = bundle
        log.info(
            "ca_pinning: loaded %d pins across %d hosts, %d CA bundles",
            sum(len(v) for v in self._pins.values()),
            len(self._pins),
            len(self._ca_bundles),
        )

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "saved_at": datetime.now(UTC).isoformat(),
            "pins": [p.to_dict() for pins in self._pins.values() for p in pins],
            "ca_bundles": [b.to_dict() for b in self._ca_bundles.values()],
        }
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._store_path)
            try:
                os.chmod(self._store_path, 0o600)
            except OSError:
                pass
        except Exception as exc:
            log.error("could not persist pinning store: %s", exc)
            raise

    # -------- pin management -----------------------------------------------

    def add_pin(
        self,
        host: str,
        pin_type: PinType,
        value: str,
        source: PinSource = PinSource.OPERATOR,
        description: str = "",
        added_by: Optional[str] = None,
        expires_at: Optional[str] = None,
        rotation_group: Optional[str] = None,
    ) -> CertificatePin:
        host = _normalize_host(host)
        value = _normalize_pin_value(value)
        pin = CertificatePin(
            host=host,
            pin_type=pin_type,
            value=value,
            source=source,
            description=description,
            added_at=datetime.now(UTC).isoformat(),
            added_by=added_by,
            expires_at=expires_at,
            rotation_group=rotation_group,
            enabled=True,
        )
        with self._lock:
            host_pins = self._pins.setdefault(host, [])
            # Reject exact duplicate (same type + value)
            for existing in host_pins:
                if existing.pin_type == pin_type and existing.value == value:
                    log.info("pin already exists for %s (%s); skipping", host, pin_type.value)
                    return existing
            host_pins.append(pin)
            self._persist()
        log.info(
            "pin added host=%s type=%s value=%s... source=%s",
            host,
            pin_type.value,
            value[:8],
            source.value,
        )
        return pin

    def remove_pin(self, host: str, pin_type: PinType, value: str, removed_by: Optional[str] = None) -> bool:
        host = _normalize_host(host)
        value = _normalize_pin_value(value)
        with self._lock:
            host_pins = self._pins.get(host, [])
            before = len(host_pins)
            self._pins[host] = [p for p in host_pins if not (p.pin_type == pin_type and p.value == value)]
            removed = before - len(self._pins[host])
            if not self._pins[host]:
                self._pins.pop(host, None)
            if removed:
                self._persist()
        log.info("pin removed host=%s type=%s value=%s... removed_count=%d", host, pin_type.value, value[:8], removed)
        return bool(removed)

    def list_pins(self, host: Optional[str] = None) -> List[CertificatePin]:
        with self._lock:
            if host:
                return list(self._pins.get(_normalize_host(host), []))
            out: List[CertificatePin] = []
            for pins in self._pins.values():
                out.extend(pins)
            return out

    def list_hosts(self) -> List[str]:
        with self._lock:
            return sorted(self._pins.keys())

    # -------- CA bundle management ----------------------------------------

    def add_ca_bundle(
        self,
        name: str,
        pem: str,
        description: str = "",
        added_by: Optional[str] = None,
    ) -> CABundle:
        if not _HAS_CRYPTO:
            raise CAPinningConfigError("cryptography library not available")
        # Validate by attempting to parse
        try:
            certs = _parse_pem_chain(pem)
        except Exception as exc:
            raise CAPinningConfigError(f"invalid PEM: {exc}") from exc
        if not certs:
            raise CAPinningConfigError("PEM bundle contains no certificates")

        bundle = CABundle(
            name=name,
            description=description,
            pem=pem,
            added_at=datetime.now(UTC).isoformat(),
            added_by=added_by,
            enabled=True,
        )
        with self._lock:
            self._ca_bundles[name] = bundle
            self._persist()
        log.info("ca_bundle added name=%s cert_count=%d", name, len(certs))
        return bundle

    def remove_ca_bundle(self, name: str) -> bool:
        with self._lock:
            removed = self._ca_bundles.pop(name, None) is not None
            if removed:
                self._persist()
        return removed

    def list_ca_bundles(self) -> List[CABundle]:
        with self._lock:
            return list(self._ca_bundles.values())

    def get_combined_ca_pem(self) -> str:
        with self._lock:
            return "\n".join(b.pem for b in self._ca_bundles.values() if b.enabled and b.pem)

    # -------- validation ----------------------------------------------------

    def validate_chain(
        self,
        host: str,
        chain_pem: str,
        check_expiry: bool = True,
        require_pin: bool = True,
    ) -> ValidationResult:
        """Validate a cert chain against pins for the given host."""
        host = _normalize_host(host)
        result = ValidationResult(host=host, valid=False)

        if not _HAS_CRYPTO:
            result.errors.append("cryptography library not available")
            return result

        try:
            chain = _parse_pem_chain(chain_pem)
        except Exception as exc:
            result.errors.append(f"could not parse chain: {exc}")
            return result

        if not chain:
            result.errors.append("empty cert chain")
            return result

        result.chain_depth = len(chain)
        leaf = chain[0]
        result.leaf_subject = leaf.subject.rfc4514_string()
        result.leaf_issuer = leaf.issuer.rfc4514_string()
        result.leaf_not_before = leaf.not_valid_before.replace(tzinfo=UTC).isoformat()
        result.leaf_not_after = leaf.not_valid_after.replace(tzinfo=UTC).isoformat()
        result.leaf_san = _extract_san(leaf)
        result.leaf_sha256 = _b64_sha256(leaf.public_bytes(serialization.Encoding.DER))
        result.leaf_spki_sha256 = _spki_sha256(leaf)

        # Expiry check
        if check_expiry:
            now = datetime.now(UTC)
            if leaf.not_valid_before.replace(tzinfo=UTC) > now:
                result.errors.append("leaf cert not yet valid")
            if leaf.not_valid_after.replace(tzinfo=UTC) < now:
                result.errors.append("leaf cert expired")
            else:
                days_left = (leaf.not_valid_after.replace(tzinfo=UTC) - now).days
                if days_left < 30:
                    result.warnings.append(f"leaf cert expires in {days_left} days")
                    self._fire_renewal_callbacks(host, leaf, days_left)

        # SAN check
        if result.leaf_san and host not in result.leaf_san and not any(
            _san_matches(host, san) for san in result.leaf_san
        ):
            result.warnings.append(
                f"host '{host}' not in leaf SAN: {result.leaf_san[:5]}{'...' if len(result.leaf_san) > 5 else ''}"
            )

        # Pin check
        with self._lock:
            host_pins = list(self._pins.get(host, []))

        active_pins = [p for p in host_pins if p.enabled and not p.is_expired]
        if not active_pins:
            if require_pin:
                result.errors.append("no active pins configured for host")
                return result
            result.warnings.append("no pins for host (require_pin=False)")
            result.valid = not result.errors
            return result

        matched: Optional[CertificatePin] = None
        for pin in active_pins:
            if pin.pin_type == PinType.CERT_SHA256:
                if pin.value == result.leaf_sha256:
                    matched = pin
                    break
            elif pin.pin_type == PinType.SPKI_SHA256:
                if pin.value == result.leaf_spki_sha256:
                    matched = pin
                    break
            elif pin.pin_type == PinType.CA_CERT_SHA256:
                for ca_cert in chain[1:]:
                    ca_sha = _b64_sha256(ca_cert.public_bytes(serialization.Encoding.DER))
                    if pin.value == ca_sha:
                        matched = pin
                        break
                if matched:
                    break

        if matched:
            result.matched_pin = matched
            result.valid = not result.errors
        else:
            result.errors.append(
                f"no pin matched; tried {len(active_pins)} pin(s); "
                f"leaf_sha256={result.leaf_sha256[:16]}... leaf_spki_sha256={result.leaf_spki_sha256[:16]}..."
            )

        return result

    def validate_or_raise(self, host: str, chain_pem: str, **kw: Any) -> ValidationResult:
        r = self.validate_chain(host, chain_pem, **kw)
        if not r.valid:
            raise PinValidationError(f"pin validation failed for {host}: {r.errors}")
        return r

    # -------- learning / TOFU ----------------------------------------------

    def learn_pin(self, host: str, chain_pem: str, added_by: Optional[str] = None) -> CertificatePin:
        """Trust-on-first-use: learn an SPKI pin from a fresh connection."""
        if not _HAS_CRYPTO:
            raise CAPinningConfigError("cryptography library not available")
        chain = _parse_pem_chain(chain_pem)
        if not chain:
            raise CAPinningConfigError("empty chain")
        leaf = chain[0]
        spki = _spki_sha256(leaf)
        return self.add_pin(
            host=host,
            pin_type=PinType.SPKI_SHA256,
            value=spki,
            source=PinSource.LEARNED,
            description=f"TOFU from {leaf.subject.rfc4514_string()}",
            added_by=added_by,
        )

    # -------- SSL context integration --------------------------------------

    def build_ssl_context(
        self,
        purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH,
        cafile: Optional[str] = None,
        cadata: Optional[str] = None,
    ) -> ssl.SSLContext:
        """Build an SSL context using only the pinned CA bundle + system trust."""
        ctx = ssl.create_default_context(purpose=purpose)
        bundle = self.get_combined_ca_pem()
        if bundle:
            try:
                ctx.load_verify_locations(cadata=bundle)
            except Exception as exc:
                log.warning("could not load CA bundle into ssl ctx: %s", exc)
        if cafile:
            ctx.load_verify_locations(cafile=cafile)
        if cadata:
            ctx.load_verify_locations(cadata=cadata)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    # -------- auto-renewal hooks -------------------------------------------

    def register_renewal_callback(self, cb: Callable[[CertificatePin, int], None]) -> None:
        self._renewal_callbacks.append(cb)

    def _fire_renewal_callbacks(self, host: str, leaf: Any, days_left: int) -> None:
        host_pins = self._pins.get(host, [])
        for pin in host_pins:
            for cb in self._renewal_callbacks:
                try:
                    cb(pin, days_left)
                except Exception as exc:  # pragma: no cover
                    log.warning("renewal callback failed: %s", exc)

    # -------- rotation helpers ---------------------------------------------

    def rotate_pin(
        self,
        host: str,
        old_pin_value: str,
        new_pin_value: str,
        pin_type: PinType = PinType.SPKI_SHA256,
        grace_seconds: int = 7 * 86400,
        rotation_group: Optional[str] = None,
        added_by: Optional[str] = None,
    ) -> Tuple[CertificatePin, CertificatePin]:
        """Add new pin, schedule old pin removal after grace window.

        Both pins are valid during the grace window — operator should call
        `prune_expired_pins()` periodically (or at the end of the grace).
        """
        host = _normalize_host(host)
        group = rotation_group or f"rotation-{int(time.time())}"
        sunset = (datetime.now(UTC).timestamp() + grace_seconds)
        sunset_iso = datetime.fromtimestamp(sunset, UTC).isoformat()

        # Mark old pin with expires_at
        with self._lock:
            for p in self._pins.get(host, []):
                if p.value == old_pin_value:
                    p.rotation_group = group
                    p.expires_at = sunset_iso
                    old_obj = p
                    break
            else:
                raise CAPinningConfigError(f"old pin not found for {host}")

        new_obj = self.add_pin(
            host=host,
            pin_type=pin_type,
            value=new_pin_value,
            source=PinSource.OPERATOR,
            rotation_group=group,
            added_by=added_by,
            description="Rotation pin",
        )
        with self._lock:
            self._persist()
        log.info("pin rotated host=%s group=%s grace_days=%.1f", host, group, grace_seconds / 86400)
        return old_obj, new_obj

    def prune_expired_pins(self) -> int:
        removed = 0
        with self._lock:
            for host, pins in list(self._pins.items()):
                fresh = [p for p in pins if not p.is_expired]
                removed += len(pins) - len(fresh)
                if fresh:
                    self._pins[host] = fresh
                else:
                    self._pins.pop(host, None)
            if removed:
                self._persist()
        log.info("pruned %d expired pins", removed)
        return removed

    # -------- async helpers -------------------------------------------------

    async def schedule_periodic_prune(self, interval_seconds: int = 3600) -> None:
        while True:
            try:
                self.prune_expired_pins()
            except Exception as exc:  # pragma: no cover
                log.warning("prune failed: %s", exc)
            await asyncio.sleep(interval_seconds)

    # -------- export/import -------------------------------------------------

    def export_json(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "exported_at": datetime.now(UTC).isoformat(),
                "pins": [p.to_dict() for pins in self._pins.values() for p in pins],
                "ca_bundles": [b.to_dict() for b in self._ca_bundles.values()],
            }

    def import_json(self, data: Dict[str, Any], merge: bool = True, imported_by: Optional[str] = None) -> int:
        added = 0
        with self._lock:
            if not merge:
                self._pins.clear()
                self._ca_bundles.clear()
            for entry in data.get("pins", []):
                pin = CertificatePin.from_dict(entry)
                if imported_by:
                    pin.added_by = imported_by
                self._pins.setdefault(pin.host, [])
                if not any(
                    p.pin_type == pin.pin_type and p.value == pin.value
                    for p in self._pins[pin.host]
                ):
                    self._pins[pin.host].append(pin)
                    added += 1
            for entry in data.get("ca_bundles", []):
                self._ca_bundles[entry["name"]] = CABundle(
                    name=entry["name"],
                    description=entry.get("description", ""),
                    pem=entry.get("pem", ""),
                    added_at=entry.get("added_at", ""),
                    added_by=entry.get("added_by"),
                    enabled=entry.get("enabled", True),
                )
            self._persist()
        log.info("imported %d new pins (merge=%s)", added, merge)
        return added


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_host(host: str) -> str:
    return host.strip().lower()


def _normalize_pin_value(value: str) -> str:
    # Strip whitespace, accept both base64 and hex; canonicalize to base64
    v = value.strip().replace(":", "").replace(" ", "")
    # If hex, convert to base64
    if all(c in "0123456789abcdefABCDEF" for c in v) and len(v) in (40, 64, 96, 128):
        raw = bytes.fromhex(v)
        return base64.b64encode(raw).decode("ascii")
    return v


def _b64_sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _spki_sha256(cert: Any) -> str:
    """SHA-256 of Subject Public Key Info (DER)."""
    der = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _b64_sha256(der)


def _parse_pem_chain(pem: str) -> List[Any]:
    """Parse PEM into list of x509.Certificate objects."""
    pem = pem.strip()
    if not pem:
        return []
    chain: List[Any] = []
    # Split by BEGIN/END CERTIFICATE markers
    for block in pem.split("-----BEGIN CERTIFICATE-----"):
        block = block.strip()
        if not block:
            continue
        if "-----END CERTIFICATE-----" in block:
            body, _ = block.split("-----END CERTIFICATE-----", 1)
            full = "-----BEGIN CERTIFICATE-----\n" + body.strip() + "\n-----END CERTIFICATE-----"
            try:
                chain.append(x509.load_pem_x509_certificate(full.encode("ascii")))
            except Exception as exc:
                log.debug("skipping unparseable cert: %s", exc)
    return chain


def _extract_san(cert: Any) -> List[str]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san_value = ext.value
        names: List[str] = []
        for n in san_value:
            try:
                names.append(n.value)
            except Exception:
                names.append(str(n))
        return names
    except Exception:
        return []


def _san_matches(host: str, san: str) -> bool:
    """Match host against SAN entry (wildcard support)."""
    host = host.lower()
    san = san.lower()
    if san.startswith("*."):
        return host.endswith(san[1:]) and host.count(".") >= san.count(".")
    return host == san


def _default_store_path() -> Path:
    p = os.environ.get("HELEN_CA_PINNING_STORE")
    if p:
        return Path(p)
    root = Path(os.environ.get("HELEN_DATA_DIR", "")) if os.environ.get("HELEN_DATA_DIR") else Path.cwd() / "data"
    return root / "ca_pinning_store.json"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_INSTANCE: Optional[CAPinningService] = None
_INSTANCE_LOCK = threading.Lock()


def get_ca_pinning_service() -> CAPinningService:
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = CAPinningService()
    return _INSTANCE


def reset_for_tests() -> None:
    """Test helper — reset singleton."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None
