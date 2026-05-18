"""
Self-signed TLS certificate generation for LAN deployments.

When `HTTPS_ENABLED=true` and no cert pair exists on disk, we mint one
here so operators don't need to run openssl by hand. The generated cert:

  * Is valid for 10 years (LAN-only, no CA chain to rotate against).
  * Lists every local interface IP as a SAN so clients binding by IP
    still get a match.
  * Always includes `localhost` and the detected hostname.

The file pair is `helen.crt` + `helen.key` by default (see `Settings`).
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _local_ips() -> list[str]:
    """Best-effort enumeration of local IPv4 addresses."""
    ips: set[str] = {"127.0.0.1"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def _build_sans(extra: list[str]) -> list[x509.GeneralName]:
    """Assemble the SAN list: localhost + hostname + all local IPs + extras."""
    names: list[x509.GeneralName] = [x509.DNSName("localhost")]
    try:
        names.append(x509.DNSName(socket.gethostname()))
    except OSError:
        pass
    for ip in _local_ips():
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            continue
    for s in extra:
        s = s.strip()
        if not s:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(s)))
        except ValueError:
            names.append(x509.DNSName(s))
    return names


def generate_self_signed(
    certfile: Path,
    keyfile: Path,
    extra_sans: list[str] | None = None,
) -> None:
    """Write a self-signed cert/key pair. Overwrites if files exist."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Helen LAN Server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Helen"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))  # skew buffer
        .not_valid_after(now + datetime.timedelta(days=365 * 10))
        .add_extension(
            x509.SubjectAlternativeName(_build_sans(extra_sans or [])),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_certificate(
    certfile: Path,
    keyfile: Path,
    extra_sans: list[str] | None = None,
) -> tuple[Path, Path]:
    """Return the cert pair, generating one if it doesn't exist yet."""
    if not (certfile.exists() and keyfile.exists()):
        generate_self_signed(certfile, keyfile, extra_sans)
    return certfile, keyfile
