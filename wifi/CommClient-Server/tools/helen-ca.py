"""
Internal CA for Helen LAN deployments.

Generates a CA root + server-leaf certificates so internal HTTPS
stops triggering "Untrusted publisher / NET::ERR_CERT_AUTHORITY_INVALID"
warnings on every browser/device. Pure Python (cryptography), no
internet dependency.

Usage::

    # 1. Bootstrap once per LAN
    python helen-ca.py bootstrap --out ~/helen-ca

    # 2. Issue a cert for a server
    python helen-ca.py issue \
        --ca-dir ~/helen-ca \
        --hostname helen-server-01.helen.lan \
        --san 10.0.0.5 --san helen.lan \
        --out ~/helen-ca/server01

    # 3. Push the CA root onto every client device:
    #      Windows: Import-Certificate -CertStoreLocation Cert:\LocalMachine\Root
    #      Linux:   sudo cp helen-ca.crt /usr/local/share/ca-certificates/
    #               && sudo update-ca-certificates
    #      macOS:   security add-trusted-cert -d -r trustRoot \
    #                  -k /Library/Keychains/System.keychain helen-ca.crt
    #      Android: Settings → Security → Install certificate
    #      iOS:     Settings → General → Profile → Install
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


# ── Helpers ─────────────────────────────────────────────────────────


def _gen_rsa(bits: int = 4096):
    if bits < 2048:
        raise ValueError(f"RSA key must be >= 2048 bits (got {bits})")
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _save_private(key, path: Path) -> None:
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _save_cert(cert, path: Path) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


# ── Bootstrap (create root CA) ──────────────────────────────────────


def cmd_bootstrap(args: argparse.Namespace) -> None:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    if (out / "helen-ca.key").exists():
        print(f"[!] {out}/helen-ca.key already exists — refusing to overwrite")
        sys.exit(1)

    print("[*] Generating 4096-bit RSA root key...")
    key = _gen_rsa(4096)

    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, args.cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, args.org),
    ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)                 # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc)
                          + dt.timedelta(days=int(args.years) * 365))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=1),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True,
                crl_sign=True,
                content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ), critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
    )
    cert = builder.sign(key, hashes.SHA256())

    _save_private(key, out / "helen-ca.key")
    _save_cert(cert, out / "helen-ca.crt")
    (out / "helen-ca.serial").write_text("1\n")
    print(f"[+] Root CA written:")
    print(f"    Private key: {out / 'helen-ca.key'}  (mode 0600)")
    print(f"    Public cert: {out / 'helen-ca.crt'}")
    print()
    print(f"  Distribute {out / 'helen-ca.crt'} to every client device")
    print(f"  (see the module docstring for OS-specific commands).")


# ── Issue (sign a server cert) ──────────────────────────────────────


def cmd_issue(args: argparse.Namespace) -> None:
    ca_dir = Path(args.ca_dir).expanduser()
    if not (ca_dir / "helen-ca.key").exists():
        print(f"[!] No CA found at {ca_dir} — run bootstrap first.")
        sys.exit(1)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    ca_key = serialization.load_pem_private_key(
        (ca_dir / "helen-ca.key").read_bytes(), password=None,
    )
    ca_cert = x509.load_pem_x509_certificate(
        (ca_dir / "helen-ca.crt").read_bytes(),
    )

    print(f"[*] Generating 2048-bit leaf key for '{args.hostname}'...")
    leaf_key = _gen_rsa(2048)

    sans: list[x509.GeneralName] = [x509.DNSName(args.hostname)]
    for s in (args.san or []):
        try:
            ip = ipaddress.ip_address(s)
            sans.append(x509.IPAddress(ip))
        except ValueError:
            sans.append(x509.DNSName(s))

    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, args.hostname),
    ])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(_next_serial(ca_dir))
        .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
        .not_valid_after(dt.datetime.now(dt.timezone.utc)
                          + dt.timedelta(days=int(args.days)))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_cert.public_key()
            ),
            critical=False,
        )
    )
    cert = builder.sign(ca_key, hashes.SHA256())

    _save_private(leaf_key, out_dir / f"{args.hostname}.key")
    _save_cert(cert, out_dir / f"{args.hostname}.crt")

    # Also write a fullchain.pem ready to drop into nginx / Helen-Server
    fullchain = (
        cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    (out_dir / f"{args.hostname}.fullchain.pem").write_bytes(fullchain)

    print(f"[+] Issued:")
    print(f"    Key:        {out_dir / f'{args.hostname}.key'}")
    print(f"    Cert:       {out_dir / f'{args.hostname}.crt'}")
    print(f"    Fullchain:  {out_dir / f'{args.hostname}.fullchain.pem'}")
    print(f"    Valid for:  {args.days} days")
    sans_str = ", ".join(args.san or []) or "(none)"
    print(f"    SANs:       {args.hostname}, {sans_str}")


def _next_serial(ca_dir: Path) -> int:
    """Allocate the next CA serial number with atomic file locking.

    Race-safe: parallel ``issue`` invocations on the same CA dir
    can't hand out duplicate serials. Uses ``msvcrt`` on Windows
    and ``fcntl`` on POSIX.
    """
    serial_path = ca_dir / "helen-ca.serial"
    serial_path.touch(exist_ok=True)
    with open(serial_path, "r+", encoding="utf-8") as f:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            content = f.read().strip()
            cur = (int(content) + 1) if content else 1
            f.seek(0)
            f.truncate()
            f.write(f"{cur}\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            if os.name == "nt":
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return cur


# ── CLI plumbing ───────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        prog="helen-ca",
        description="Helen internal CA — bootstrap a root + issue server certs",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="generate a root CA")
    b.add_argument("--out", default="./helen-ca",
                   help="directory to write helen-ca.{key,crt}")
    b.add_argument("--cn", default="Helen Internal Root CA")
    b.add_argument("--org", default="Helen Project")
    b.add_argument("--years", type=int, default=20,
                   help="root validity (default: 20 years)")
    b.set_defaults(func=cmd_bootstrap)

    i = sub.add_parser("issue", help="issue a server-leaf certificate")
    i.add_argument("--ca-dir", default="./helen-ca")
    i.add_argument("--hostname", required=True,
                   help="primary CN/SAN, e.g. helen-server-01.helen.lan")
    i.add_argument("--san", action="append", default=[],
                   help="additional SAN (repeatable; IP or DNS)")
    i.add_argument("--days", type=int, default=825,
                   help="leaf validity (default 825 days, OS max trust)")
    i.add_argument("--out", default="./helen-ca/leaf")
    i.set_defaults(func=cmd_issue)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
