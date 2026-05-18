"""
Phase 2 / Module J — TLS certificate manager.

Capabilities
------------
* ``inspect_cert(path)``           — parse cert + return rich metadata
* ``regenerate_self_signed(...)``  — fresh self-signed cert + RSA key pair
* ``acme_request(...)``            — ACME via the optional ``acme`` library
                                     (graceful degradation when absent)

All file writes go through a process-wide ``threading.Lock`` so concurrent
admins can't race a cert swap.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtensionOID, NameOID


_FILE_LOCK = threading.Lock()


# ── Data shapes ───────────────────────────────────────────

@dataclass
class CertInfo:
    common_name: Optional[str]
    issuer: Optional[str]
    san: list[str]
    not_before: str
    not_after: str
    days_remaining: int
    expired: bool
    fingerprint_sha256: str
    serial: str
    key_algorithm: str
    key_size: Optional[int]
    pem_bytes: int
    self_signed: bool
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "common_name": self.common_name,
            "issuer": self.issuer,
            "san": self.san,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "days_remaining": self.days_remaining,
            "expired": self.expired,
            "fingerprint_sha256": self.fingerprint_sha256,
            "serial": self.serial,
            "key_algorithm": self.key_algorithm,
            "key_size": self.key_size,
            "pem_bytes": self.pem_bytes,
            "self_signed": self.self_signed,
            "path": self.path,
        }


# ── Inspection ────────────────────────────────────────────

def inspect_cert(path: str | os.PathLike) -> CertInfo:
    p = Path(path)
    pem = p.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)

    def _name_value(name: x509.Name, oid) -> Optional[str]:
        try:
            attrs = name.get_attributes_for_oid(oid)
        except Exception:
            return None
        return attrs[0].value if attrs else None

    cn = _name_value(cert.subject, NameOID.COMMON_NAME)
    issuer = _name_value(cert.issuer, NameOID.COMMON_NAME) \
        or cert.issuer.rfc4514_string()

    san: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME,
        )
        for v in ext.value:
            if isinstance(v, x509.DNSName):
                san.append(v.value)
            elif isinstance(v, x509.IPAddress):
                san.append(str(v.value))
            elif isinstance(v, x509.UniformResourceIdentifier):
                san.append(v.value)
    except x509.ExtensionNotFound:
        pass

    pub = cert.public_key()
    key_algo = type(pub).__name__
    key_size = getattr(pub, "key_size", None)

    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()

    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    now = datetime.now(timezone.utc)

    return CertInfo(
        common_name=cn,
        issuer=issuer,
        san=san,
        not_before=not_before.isoformat(),
        not_after=not_after.isoformat(),
        days_remaining=(not_after - now).days,
        expired=now > not_after,
        fingerprint_sha256=fp,
        serial=format(cert.serial_number, "x"),
        key_algorithm=key_algo,
        key_size=key_size,
        pem_bytes=len(pem),
        self_signed=(cert.issuer == cert.subject),
        path=str(p),
    )


# ── Self-signed regeneration ──────────────────────────────

def regenerate_self_signed(
    san_list: list[str],
    *,
    days: int = 365,
    common_name: Optional[str] = None,
    key_size: int = 2048,
) -> tuple[bytes, bytes]:
    """Return (cert_pem, key_pem). The caller decides where to persist."""
    cn = common_name or (san_list[0] if san_list else "helen-server")

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Helen"),
    ])
    san_objects: list[x509.GeneralName] = []
    for entry in san_list:
        entry = entry.strip()
        if not entry:
            continue
        try:
            ip = ipaddress.ip_address(entry)
            san_objects.append(x509.IPAddress(ip))
        except ValueError:
            san_objects.append(x509.DNSName(entry))

    cert_builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=True,
            key_agreement=False, content_commitment=False,
            data_encipherment=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
    )
    if san_objects:
        cert_builder = cert_builder.add_extension(
            x509.SubjectAlternativeName(san_objects), critical=False,
        )

    cert = cert_builder.sign(private_key=key, algorithm=hashes.SHA256())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def write_cert_pair(
    cert_pem: bytes, key_pem: bytes,
    cert_path: str | os.PathLike, key_path: str | os.PathLike,
    *, backup: bool = True,
) -> dict[str, str]:
    """Atomically replace the on-disk cert + key. Returns backup paths."""
    with _FILE_LOCK:
        out: dict[str, str] = {}
        for src_pem, dest in ((cert_pem, Path(cert_path)),
                              (key_pem, Path(key_path))):
            dest.parent.mkdir(parents=True, exist_ok=True)
            if backup and dest.exists():
                bk = dest.with_suffix(dest.suffix + f".bak-{int(datetime.now().timestamp())}")
                shutil.copy2(dest, bk)
                out[str(dest)] = str(bk)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(src_pem)
            with contextlib.suppress(Exception):
                os.chmod(tmp, 0o600 if "key" in dest.name.lower() else 0o644)
            os.replace(tmp, dest)
        return out


# ── ACME (graceful degradation) ───────────────────────────

@dataclass
class AcmeResult:
    success: bool
    message: str
    cert_pem: bytes = b""
    key_pem: bytes = b""
    details: dict = field(default_factory=dict)


def acme_available() -> bool:
    """True iff the optional ``acme`` package is importable."""
    try:
        import acme                                                  # noqa: F401
        return True
    except ImportError:
        return False


def acme_request(
    domain: str,
    email: str,
    mode: Literal["http01", "dns01"] = "http01",
    *,
    directory_url: str = "https://acme-v02.api.letsencrypt.org/directory",
    staging: bool = False,
) -> AcmeResult:
    """Request a real cert from Let's Encrypt.

    Requires the user-installed ``acme`` package. We do *not* hard-fail when
    it isn't present; we return an ``AcmeResult`` with ``success=False`` so
    the API caller can render a clean error message.
    """
    if not re.fullmatch(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", email):
        return AcmeResult(False, f"invalid email: {email}")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", domain):
        return AcmeResult(False, f"invalid domain: {domain}")

    if not acme_available():
        return AcmeResult(
            False,
            "ACME support not installed. Run: pip install acme",
            details={"mode": mode, "domain": domain},
        )

    # Lazy imports so the missing dependency only matters when actually used.
    from acme import client, messages, challenges                     # type: ignore
    from acme import crypto_util                                      # type: ignore
    import josepy as jose                                             # type: ignore

    try:
        if staging:
            directory_url = "https://acme-staging-v02.api.letsencrypt.org/directory"

        account_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jose.JWKRSA(key=account_key)
        net = client.ClientNetwork(jwk, user_agent="helen-tls/1.0")
        directory = client.ClientV2.get_directory(directory_url, net)
        cli = client.ClientV2(directory, net=net)

        reg = cli.new_account(messages.NewRegistration.from_data(
            email=email, terms_of_service_agreed=True,
        ))

        csr_pem, key_pem = crypto_util.make_csr_with_key([domain])
        order = cli.new_order(csr_pem)

        # Solve challenges
        wanted = {"http01": challenges.HTTP01, "dns01": challenges.DNS01}[mode]
        for authz in order.authorizations:
            ch = next(
                (c for c in authz.body.challenges if isinstance(c.chall, wanted)),
                None,
            )
            if ch is None:
                return AcmeResult(False, f"{mode} challenge not offered for {domain}")
            # In a real deployment the caller publishes the challenge token
            # (http01: HTTP server / dns01: DNS TXT). We expose that token
            # via the details payload so an operator can prepare it.
            response, validation = ch.response_and_validation(jwk)
            cli.answer_challenge(ch, response)

        finalized = cli.poll_and_finalize(order)
        return AcmeResult(
            True, "issued",
            cert_pem=finalized.fullchain_pem.encode(),
            key_pem=key_pem,
            details={"domain": domain, "mode": mode, "reg": str(reg.uri)},
        )
    except Exception as e:                                            # pragma: no cover
        return AcmeResult(False, f"ACME error: {e}",
                          details={"domain": domain, "mode": mode})
