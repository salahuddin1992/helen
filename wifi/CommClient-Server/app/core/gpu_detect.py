"""
Hardware encoder detection.

Best-effort probe for available hardware encoders so the ingest service can
pick NVENC > QuickSync > AMF > libx264 without the operator editing config.

The probe is cheap (ffmpeg -encoders / nvidia-smi) and cached for the life
of the process — encoder availability doesn't change at runtime short of
unplugging a GPU.

Public surface
--------------
    gpu_detect.probe() -> GpuCapabilities
    gpu_detect.preferred_video_encoder(codec="h264") -> str

Returns ffmpeg encoder names ready to pass to `-c:v`:
    h264_nvenc, hevc_nvenc, h264_qsv, hevc_qsv, h264_amf, hevc_amf,
    libx264, libx265.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GpuCapabilities:
    has_nvidia: bool = False
    has_intel_qsv: bool = False
    has_amd_amf: bool = False
    ffmpeg_path: str | None = None
    ffmpeg_version: str | None = None
    nvidia_driver: str | None = None
    available_encoders: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "has_nvidia": self.has_nvidia,
            "has_intel_qsv": self.has_intel_qsv,
            "has_amd_amf": self.has_amd_amf,
            "ffmpeg_path": self.ffmpeg_path,
            "ffmpeg_version": self.ffmpeg_version,
            "nvidia_driver": self.nvidia_driver,
            "available_encoders": self.available_encoders,
        }


def _run_short(cmd: list[str], timeout: float = 4.0) -> tuple[int, str]:
    """Run a short command, capture combined output, never raise."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception as e:
        return 1, str(e)


def _probe_ffmpeg(ffmpeg_bin: str) -> tuple[str | None, list[str]]:
    """Return (version, encoder_names) from `ffmpeg -encoders`."""
    rc, out = _run_short([ffmpeg_bin, "-hide_banner", "-encoders"], timeout=5.0)
    if rc != 0:
        return None, []
    # Each encoder line looks like " V..... h264_nvenc  NVIDIA NVENC H.264 encoder"
    encoders: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("-") or s.startswith("Encoders:"):
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        flags = parts[0]
        name = parts[1]
        if flags and flags[0] in ("V", "A", "S"):
            encoders.append(name)

    rc_v, out_v = _run_short([ffmpeg_bin, "-hide_banner", "-version"], timeout=3.0)
    version = None
    if rc_v == 0 and out_v:
        first = out_v.splitlines()[0] if out_v.splitlines() else ""
        version = first.strip() or None

    return version, encoders


def _probe_nvidia() -> str | None:
    """Return the nvidia-smi driver version, or None if missing."""
    bin_path = shutil.which("nvidia-smi")
    if not bin_path:
        return None
    rc, out = _run_short(
        [bin_path, "--query-gpu=driver_version", "--format=csv,noheader"],
        timeout=3.0,
    )
    if rc != 0:
        return None
    driver = (out or "").strip().splitlines()
    return driver[0].strip() if driver else None


_NVENC_NAMES = {
    "h264_nvenc", "hevc_nvenc", "av1_nvenc",
}
_QSV_NAMES = {
    "h264_qsv", "hevc_qsv", "av1_qsv",
}
_AMF_NAMES = {
    "h264_amf", "hevc_amf", "av1_amf",
}


@lru_cache(maxsize=1)
def probe() -> GpuCapabilities:
    """Probe once per process. Cached."""
    caps = GpuCapabilities()

    ffmpeg_path = shutil.which("ffmpeg")
    caps.ffmpeg_path = ffmpeg_path

    if ffmpeg_path:
        version, encoders = _probe_ffmpeg(ffmpeg_path)
        caps.ffmpeg_version = version
        caps.available_encoders = encoders

        enc_set = set(encoders)
        caps.has_nvidia = bool(enc_set & _NVENC_NAMES)
        caps.has_intel_qsv = bool(enc_set & _QSV_NAMES)
        caps.has_amd_amf = bool(enc_set & _AMF_NAMES)
    else:
        logger.warning("gpu_detect_ffmpeg_missing", message="ffmpeg not on PATH; transcoding unavailable")

    caps.nvidia_driver = _probe_nvidia()

    logger.info(
        "gpu_detected",
        nvidia=caps.has_nvidia,
        qsv=caps.has_intel_qsv,
        amf=caps.has_amd_amf,
        ffmpeg=bool(caps.ffmpeg_path),
        driver=caps.nvidia_driver,
    )
    return caps


def preferred_video_encoder(codec: str = "h264", prefer_hw: bool = True) -> str:
    """
    Pick the best encoder available for the requested codec.
    Falls back to software (libx264/libx265) when no HW encoder is present
    or when prefer_hw is False.
    """
    caps = probe()
    codec = codec.lower()

    if prefer_hw:
        if codec in ("h264", "avc"):
            if caps.has_nvidia and "h264_nvenc" in caps.available_encoders:
                return "h264_nvenc"
            if caps.has_intel_qsv and "h264_qsv" in caps.available_encoders:
                return "h264_qsv"
            if caps.has_amd_amf and "h264_amf" in caps.available_encoders:
                return "h264_amf"
        elif codec in ("hevc", "h265"):
            if caps.has_nvidia and "hevc_nvenc" in caps.available_encoders:
                return "hevc_nvenc"
            if caps.has_intel_qsv and "hevc_qsv" in caps.available_encoders:
                return "hevc_qsv"
            if caps.has_amd_amf and "hevc_amf" in caps.available_encoders:
                return "hevc_amf"
        elif codec == "av1":
            if caps.has_nvidia and "av1_nvenc" in caps.available_encoders:
                return "av1_nvenc"
            if caps.has_intel_qsv and "av1_qsv" in caps.available_encoders:
                return "av1_qsv"
            if caps.has_amd_amf and "av1_amf" in caps.available_encoders:
                return "av1_amf"

    # Software fallbacks.
    if codec in ("hevc", "h265"):
        return "libx265"
    if codec == "av1" and "libsvtav1" in caps.available_encoders:
        return "libsvtav1"
    return "libx264"


async def aprobe() -> GpuCapabilities:
    """Async wrapper for use in route handlers."""
    return await asyncio.to_thread(probe)


__all__ = ["GpuCapabilities", "probe", "aprobe", "preferred_video_encoder"]
