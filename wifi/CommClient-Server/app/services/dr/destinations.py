"""
Pluggable backup-destination adapters.

Every adapter implements the same async-friendly interface::

    upload(path: Path, key: str)             -> dict (metadata)
    download(key: str, dest: Path)           -> Path
    list(prefix: str = "")                   -> list[dict]
    delete(key: str)                         -> bool
    verify(key: str, sha256: str | None)     -> bool
    health()                                 -> dict

All providers degrade gracefully when their optional SDK dependency is
missing: ``LocalDestination`` is always available; the others raise a
clean ``RuntimeError("<sdk> not installed")`` only at instantiation.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── protocol ────────────────────────────────────────────────────


class BackupDestination(Protocol):
    kind: str

    async def upload(self, path: Path, key: str) -> Dict[str, Any]: ...
    async def download(self, key: str, dest: Path) -> Path: ...
    async def list(self, prefix: str = "") -> List[Dict[str, Any]]: ...
    async def delete(self, key: str) -> bool: ...
    async def verify(self, key: str, sha256: Optional[str] = None) -> bool: ...
    async def health(self) -> Dict[str, Any]: ...


def _sha256_file(p: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ── local FS ────────────────────────────────────────────────────


@dataclass
class LocalDestination:
    kind: str = "local"
    root: Path = field(default_factory=lambda: Path("data/dr/uploads"))

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full(self, key: str) -> Path:
        return self.root / key

    async def upload(self, path: Path, key: str) -> Dict[str, Any]:
        dst = self._full(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, str(path), str(dst))
        return {"key": key, "size": dst.stat().st_size, "path": str(dst)}

    async def download(self, key: str, dest: Path) -> Path:
        src = self._full(key)
        if not src.exists():
            raise FileNotFoundError(key)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copy2, str(src), str(dest))
        return dest

    async def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        base = self._full(prefix)
        out: List[Dict[str, Any]] = []
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    out.append({
                        "key": str(p.relative_to(self.root)),
                        "size": p.stat().st_size,
                        "mtime": p.stat().st_mtime,
                    })
        elif base.is_file():
            out.append({"key": prefix, "size": base.stat().st_size,
                        "mtime": base.stat().st_mtime})
        return out

    async def delete(self, key: str) -> bool:
        p = self._full(key)
        if p.exists():
            await asyncio.to_thread(p.unlink, True)
            return True
        return False

    async def verify(self, key: str, sha256: Optional[str] = None) -> bool:
        p = self._full(key)
        if not p.exists():
            return False
        if not sha256:
            return True
        return await asyncio.to_thread(_sha256_file, p) == sha256

    async def health(self) -> Dict[str, Any]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            test = self.root / ".healthcheck"
            test.write_text("ok")
            test.unlink(missing_ok=True)
            return {"ok": True, "kind": self.kind, "root": str(self.root)}
        except Exception as e:
            return {"ok": False, "kind": self.kind, "error": str(e)}


# ── S3 ──────────────────────────────────────────────────────────


try:                                                                 # pragma: no cover
    import boto3                                                     # type: ignore
    from botocore.exceptions import ClientError                      # type: ignore
    _BOTO_OK = True
except Exception:                                                    # pragma: no cover
    boto3 = None                                                     # type: ignore
    ClientError = Exception                                          # type: ignore
    _BOTO_OK = False


@dataclass
class S3Destination:
    kind: str = "s3"
    bucket: str = ""
    prefix: str = ""
    region: Optional[str] = None
    endpoint_url: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not _BOTO_OK:
            raise RuntimeError("boto3 not installed — S3 destination unavailable")
        if not self.bucket:
            raise ValueError("S3 destination requires a bucket name")
        self._client = boto3.client(                                 # type: ignore[attr-defined]
            "s3",
            region_name=self.region,
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def _key(self, key: str) -> str:
        if not self.prefix:
            return key
        return self.prefix.rstrip("/") + "/" + key.lstrip("/")

    async def upload(self, path: Path, key: str) -> Dict[str, Any]:
        full = self._key(key)
        await asyncio.to_thread(self._client.upload_file, str(path), self.bucket, full)
        return {"key": key, "bucket": self.bucket, "size": path.stat().st_size}

    async def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self._client.download_file, self.bucket, self._key(key), str(dest),
        )
        return dest

    async def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        full = self._key(prefix) if prefix else (self.prefix or "")
        resp = await asyncio.to_thread(
            self._client.list_objects_v2, Bucket=self.bucket, Prefix=full,
        )
        return [
            {"key": o["Key"], "size": o.get("Size", 0),
             "mtime": o.get("LastModified").timestamp() if o.get("LastModified") else 0}
            for o in resp.get("Contents", []) or []
        ]

    async def delete(self, key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self.bucket, Key=self._key(key),
            )
            return True
        except ClientError:
            return False

    async def verify(self, key: str, sha256: Optional[str] = None) -> bool:
        try:
            head = await asyncio.to_thread(
                self._client.head_object, Bucket=self.bucket, Key=self._key(key),
            )
            return bool(head)
        except ClientError:
            return False

    async def health(self) -> Dict[str, Any]:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            return {"ok": True, "kind": self.kind, "bucket": self.bucket}
        except Exception as e:
            return {"ok": False, "kind": self.kind, "error": str(e)}


# ── SFTP ────────────────────────────────────────────────────────


try:                                                                 # pragma: no cover
    import paramiko                                                  # type: ignore
    _PARAMIKO_OK = True
except Exception:                                                    # pragma: no cover
    paramiko = None                                                  # type: ignore
    _PARAMIKO_OK = False


@dataclass
class SFTPDestination:
    kind: str = "sftp"
    host: str = ""
    port: int = 22
    user: str = ""
    password: Optional[str] = None
    key_path: Optional[str] = None
    remote_root: str = "/backups"

    def __post_init__(self) -> None:
        if not _PARAMIKO_OK:
            raise RuntimeError("paramiko not installed — SFTP destination unavailable")
        if not self.host or not self.user:
            raise ValueError("SFTP destination requires host and user")

    def _connect(self):
        t = paramiko.Transport((self.host, self.port))                # type: ignore[attr-defined]
        if self.key_path:
            pkey = paramiko.RSAKey.from_private_key_file(self.key_path)  # type: ignore[attr-defined]
            t.connect(username=self.user, pkey=pkey)
        else:
            t.connect(username=self.user, password=self.password)
        return paramiko.SFTPClient.from_transport(t), t               # type: ignore[attr-defined]

    def _remote(self, key: str) -> str:
        return self.remote_root.rstrip("/") + "/" + key.lstrip("/")

    async def upload(self, path: Path, key: str) -> Dict[str, Any]:
        def _do():
            sftp, t = self._connect()
            try:
                # mkdir -p
                parts = self._remote(key).split("/")
                cur = ""
                for part in parts[:-1]:
                    if not part:
                        continue
                    cur += "/" + part
                    try:
                        sftp.stat(cur)
                    except IOError:
                        sftp.mkdir(cur)
                sftp.put(str(path), self._remote(key))
            finally:
                try:
                    sftp.close()
                finally:
                    t.close()
        await asyncio.to_thread(_do)
        return {"key": key, "host": self.host, "size": path.stat().st_size}

    async def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        def _do():
            sftp, t = self._connect()
            try:
                sftp.get(self._remote(key), str(dest))
            finally:
                try:
                    sftp.close()
                finally:
                    t.close()
        await asyncio.to_thread(_do)
        return dest

    async def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        def _do():
            sftp, t = self._connect()
            try:
                root = self._remote(prefix or ".")
                out: List[Dict[str, Any]] = []
                try:
                    for attr in sftp.listdir_attr(root):
                        out.append({
                            "key": (prefix.rstrip("/") + "/" if prefix else "") + attr.filename,
                            "size": attr.st_size or 0,
                            "mtime": attr.st_mtime or 0,
                        })
                except IOError:
                    pass
                return out
            finally:
                try:
                    sftp.close()
                finally:
                    t.close()
        return await asyncio.to_thread(_do)

    async def delete(self, key: str) -> bool:
        def _do():
            sftp, t = self._connect()
            try:
                try:
                    sftp.remove(self._remote(key))
                    return True
                except IOError:
                    return False
            finally:
                try:
                    sftp.close()
                finally:
                    t.close()
        return await asyncio.to_thread(_do)

    async def verify(self, key: str, sha256: Optional[str] = None) -> bool:
        def _do():
            sftp, t = self._connect()
            try:
                try:
                    sftp.stat(self._remote(key))
                    return True
                except IOError:
                    return False
            finally:
                try:
                    sftp.close()
                finally:
                    t.close()
        return await asyncio.to_thread(_do)

    async def health(self) -> Dict[str, Any]:
        try:
            sftp, t = await asyncio.to_thread(self._connect)
            try:
                return {"ok": True, "kind": self.kind, "host": self.host}
            finally:
                sftp.close()
                t.close()
        except Exception as e:
            return {"ok": False, "kind": self.kind, "error": str(e)}


# ── Azure Blob ──────────────────────────────────────────────────


try:                                                                 # pragma: no cover
    from azure.storage.blob import BlobServiceClient                 # type: ignore
    _AZURE_OK = True
except Exception:                                                    # pragma: no cover
    BlobServiceClient = None                                         # type: ignore
    _AZURE_OK = False


@dataclass
class AzureBlobDestination:
    kind: str = "azure_blob"
    connection_string: str = ""
    container: str = ""
    prefix: str = ""

    def __post_init__(self) -> None:
        if not _AZURE_OK:
            raise RuntimeError(
                "azure-storage-blob not installed — Azure destination unavailable",
            )
        if not self.connection_string or not self.container:
            raise ValueError("Azure destination requires connection_string + container")
        self._svc = BlobServiceClient.from_connection_string(           # type: ignore[attr-defined]
            self.connection_string,
        )
        self._container = self._svc.get_container_client(self.container)

    def _key(self, key: str) -> str:
        return self.prefix.rstrip("/") + "/" + key.lstrip("/") if self.prefix else key

    async def upload(self, path: Path, key: str) -> Dict[str, Any]:
        def _do():
            with open(path, "rb") as f:
                self._container.upload_blob(name=self._key(key), data=f, overwrite=True)
        await asyncio.to_thread(_do)
        return {"key": key, "container": self.container, "size": path.stat().st_size}

    async def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        def _do():
            stream = self._container.download_blob(self._key(key))
            with open(dest, "wb") as f:
                f.write(stream.readall())
        await asyncio.to_thread(_do)
        return dest

    async def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        def _do():
            full = self._key(prefix) if prefix else (self.prefix or None)
            out: List[Dict[str, Any]] = []
            for b in self._container.list_blobs(name_starts_with=full):
                out.append({"key": b.name, "size": b.size or 0,
                            "mtime": b.last_modified.timestamp() if b.last_modified else 0})
            return out
        return await asyncio.to_thread(_do)

    async def delete(self, key: str) -> bool:
        def _do():
            try:
                self._container.delete_blob(self._key(key))
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_do)

    async def verify(self, key: str, sha256: Optional[str] = None) -> bool:
        def _do():
            try:
                self._container.get_blob_client(self._key(key)).get_blob_properties()
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_do)

    async def health(self) -> Dict[str, Any]:
        try:
            await asyncio.to_thread(self._container.get_container_properties)
            return {"ok": True, "kind": self.kind, "container": self.container}
        except Exception as e:
            return {"ok": False, "kind": self.kind, "error": str(e)}


# ── GCS ─────────────────────────────────────────────────────────


try:                                                                 # pragma: no cover
    from google.cloud import storage as gcs_storage                  # type: ignore
    _GCS_OK = True
except Exception:                                                    # pragma: no cover
    gcs_storage = None                                               # type: ignore
    _GCS_OK = False


@dataclass
class GCSDestination:
    kind: str = "gcs"
    bucket: str = ""
    prefix: str = ""
    credentials_path: Optional[str] = None

    def __post_init__(self) -> None:
        if not _GCS_OK:
            raise RuntimeError(
                "google-cloud-storage not installed — GCS destination unavailable",
            )
        if not self.bucket:
            raise ValueError("GCS destination requires a bucket name")
        if self.credentials_path:
            self._client = gcs_storage.Client.from_service_account_json(  # type: ignore[attr-defined]
                self.credentials_path,
            )
        else:
            self._client = gcs_storage.Client()                           # type: ignore[attr-defined]
        self._bucket = self._client.bucket(self.bucket)

    def _key(self, key: str) -> str:
        return self.prefix.rstrip("/") + "/" + key.lstrip("/") if self.prefix else key

    async def upload(self, path: Path, key: str) -> Dict[str, Any]:
        def _do():
            blob = self._bucket.blob(self._key(key))
            blob.upload_from_filename(str(path))
        await asyncio.to_thread(_do)
        return {"key": key, "bucket": self.bucket, "size": path.stat().st_size}

    async def download(self, key: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        def _do():
            blob = self._bucket.blob(self._key(key))
            blob.download_to_filename(str(dest))
        await asyncio.to_thread(_do)
        return dest

    async def list(self, prefix: str = "") -> List[Dict[str, Any]]:
        def _do():
            full = self._key(prefix) if prefix else (self.prefix or None)
            out: List[Dict[str, Any]] = []
            for b in self._bucket.list_blobs(prefix=full):
                out.append({"key": b.name, "size": b.size or 0,
                            "mtime": b.time_created.timestamp() if b.time_created else 0})
            return out
        return await asyncio.to_thread(_do)

    async def delete(self, key: str) -> bool:
        def _do():
            try:
                self._bucket.blob(self._key(key)).delete()
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_do)

    async def verify(self, key: str, sha256: Optional[str] = None) -> bool:
        def _do():
            try:
                return self._bucket.blob(self._key(key)).exists()
            except Exception:
                return False
        return await asyncio.to_thread(_do)

    async def health(self) -> Dict[str, Any]:
        try:
            await asyncio.to_thread(lambda: self._bucket.exists())
            return {"ok": True, "kind": self.kind, "bucket": self.bucket}
        except Exception as e:
            return {"ok": False, "kind": self.kind, "error": str(e)}


# ── factory ─────────────────────────────────────────────────────


def build_destination(kind: str, config: Dict[str, Any]) -> BackupDestination:
    """Return a destination adapter for the given kind + JSON config."""
    k = (kind or "local").lower()
    cfg = dict(config or {})
    if k == "local":
        return LocalDestination(root=Path(cfg.get("root", "data/dr/uploads")))
    if k == "s3":
        return S3Destination(
            bucket=cfg["bucket"], prefix=cfg.get("prefix", ""),
            region=cfg.get("region"), endpoint_url=cfg.get("endpoint_url"),
            access_key=cfg.get("access_key"), secret_key=cfg.get("secret_key"),
        )
    if k == "sftp":
        return SFTPDestination(
            host=cfg["host"], port=int(cfg.get("port", 22)),
            user=cfg["user"], password=cfg.get("password"),
            key_path=cfg.get("key_path"), remote_root=cfg.get("remote_root", "/backups"),
        )
    if k == "azure_blob":
        return AzureBlobDestination(
            connection_string=cfg["connection_string"],
            container=cfg["container"], prefix=cfg.get("prefix", ""),
        )
    if k == "gcs":
        return GCSDestination(
            bucket=cfg["bucket"], prefix=cfg.get("prefix", ""),
            credentials_path=cfg.get("credentials_path"),
        )
    raise ValueError(f"unknown destination kind: {kind!r}")


def installed_destinations() -> Dict[str, bool]:
    """Return which optional destination SDKs are available."""
    return {
        "local": True,
        "s3": _BOTO_OK,
        "sftp": _PARAMIKO_OK,
        "azure_blob": _AZURE_OK,
        "gcs": _GCS_OK,
    }
