"""
MinIO / on-prem S3 driver — AWS public hosts are HARD-BLOCKED.

The DR Console is LAN-only by policy.  This driver wraps the S3 wire
protocol but refuses to talk to any of the well-known public cloud
endpoints, including AWS S3, Wasabi, GCS-S3-interop, Azure-Blob, DO
Spaces, and Backblaze B2-S3 — anything that looks like it could leave
the operator network.
"""
from __future__ import annotations

import asyncio
import io
import time
from typing import Any, Dict, List
from urllib.parse import urlparse

from app.core.logging import get_logger

from .base import DRDestinationDriver, DriverHealth, DriverWriteResult


logger = get_logger(__name__)


# Public hosts banned from ever being used as a DR destination.
# Substring match against the lower-cased URL netloc.
_PUBLIC_BLOCKLIST = (
    "amazonaws.com",
    "aws.com",
    "googleapis.com",
    "storage.cloud.google.com",
    "blob.core.windows.net",
    "core.windows.net",
    "core.usgovcloudapi.net",
    "core.chinacloudapi.cn",
    "wasabisys.com",
    "digitaloceanspaces.com",
    "backblazeb2.com",
    "linodeobjects.com",
    "objects-us",
    "ovh.us",
    "ovh.net",
    "cloud.it",
    "scaleway.com",
    "tebi.io",
)


def _assert_lan_endpoint(endpoint_url: str | None) -> None:
    if not endpoint_url:
        raise ValueError(
            "minio-s3-onprem destination requires an explicit endpoint_url "
            "pointing at a LAN MinIO/SeaweedFS server.",
        )
    parsed = urlparse(endpoint_url)
    host = (parsed.netloc or parsed.path).lower()
    for bad in _PUBLIC_BLOCKLIST:
        if bad in host:
            raise ValueError(
                f"endpoint host {host!r} matches public cloud blocklist "
                f"({bad!r}) — DR destinations must be LAN-only.",
            )


try:  # pragma: no cover — optional dep
    import boto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore
    _BOTO_OK = True
except Exception:
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore
    _BOTO_OK = False


class MinIOS3Driver(DRDestinationDriver):
    kind = "minio-s3-onprem"

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
        prefix: str = "",
        verify_tls: bool = True,
        **_: Any,
    ) -> None:
        _assert_lan_endpoint(endpoint_url)
        if not _BOTO_OK:
            raise RuntimeError("boto3 not installed — minio-s3 destination unavailable")
        if not bucket:
            raise ValueError("minio-s3 destination requires bucket")
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(  # type: ignore[union-attr]
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            verify=verify_tls,
        )

    def _key(self, prefix: str, seq: int | None = None) -> str:
        parts = []
        if self.prefix:
            parts.append(self.prefix)
        parts.append(prefix.strip("/").replace("..", "_"))
        if seq is not None:
            parts.append(f"chunk_{seq:08d}.bin")
        return "/".join(parts)

    async def write_chunk(
        self, prefix: str, seq: int, data: bytes, *, sha256: str,
    ) -> DriverWriteResult:
        t0 = time.perf_counter()
        key = self._key(prefix, seq)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self.bucket, Key=key, Body=data,
            Metadata={"sha256": sha256},
        )
        dt = (time.perf_counter() - t0) * 1000.0
        return DriverWriteResult(
            storage_key=key, bytes_written=len(data),
            sha256=sha256, duration_ms=dt, encrypted_size=len(data),
        )

    async def read_chunk(self, prefix: str, seq: int) -> bytes:
        key = self._key(prefix, seq)
        def _do() -> bytes:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return resp["Body"].read()
        return await asyncio.to_thread(_do)

    async def list_objects(self, prefix: str = "") -> List[Dict[str, Any]]:
        full = self._key(prefix) if prefix else self.prefix
        def _do():
            resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=full)
            return [
                {"key": o["Key"], "size": o.get("Size", 0),
                 "mtime": o.get("LastModified").timestamp() if o.get("LastModified") else 0}
                for o in resp.get("Contents", []) or []
            ]
        return await asyncio.to_thread(_do)

    async def delete(self, prefix: str) -> bool:
        full = self._key(prefix)
        def _do():
            try:
                resp = self._client.list_objects_v2(Bucket=self.bucket, Prefix=full)
                keys = [{"Key": o["Key"]} for o in resp.get("Contents", []) or []]
                if not keys:
                    self._client.delete_object(Bucket=self.bucket, Key=full)
                    return True
                self._client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": keys},
                )
                return True
            except ClientError:
                return False
        return await asyncio.to_thread(_do)

    async def capacity(self) -> Dict[str, int]:
        # MinIO exposes capacity via admin API; in v1 we report unknown.
        return {"capacity_bytes": 0, "used_bytes": 0, "free_bytes": 0}

    async def test(self) -> DriverHealth:
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            dt = (time.perf_counter() - t0) * 1000.0
            return DriverHealth(
                ok=True, kind=self.kind, latency_ms=dt,
                details={"endpoint": self.endpoint_url, "bucket": self.bucket},
            )
        except Exception as e:
            return DriverHealth(ok=False, kind=self.kind, error=str(e))
