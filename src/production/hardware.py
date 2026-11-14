"""
src/production/hardware.py
Encoder detection, catalog probing, and runtime smoke testing for hardware acceleration.
Ensures zero-crash execution on Kaggle (NVENC), macOS (VideoToolbox), and CPU (libx264).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Dict, List, Optional, Tuple

from src.core.logger import logger

SUPPORTED_ENCODERS: List[str] = ["auto", "libx264", "h264_nvenc", "h264_videotoolbox", "nvenc", "videotoolbox"]


def probe_available_encoders() -> List[str]:
    """
    Queries ffmpeg -encoders to find compiled video encoders.
    Returns list of discovered video encoder names (e.g. ['libx264', 'h264_videotoolbox']).
    """
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg binary not found in system PATH.")
        return []

    try:
        res = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        found: List[str] = []
        for line in res.stdout.splitlines():
            line_str = line.strip()
            # Look for video encoders: line starts with V (e.g., ' V..... libx264')
            if line_str.startswith("V") and ("264" in line_str or "h264" in line_str):
                parts = line_str.split()
                if len(parts) >= 2:
                    found.append(parts[1])
        return found
    except Exception as e:
        logger.warning(f"Failed to probe ffmpeg encoders: {e}")
        return []


def probe_ffmpeg_capabilities(ffmpeg_bin: str = "ffmpeg") -> Dict[str, bool]:
    """Probes FFmpeg encoders and filters needed by P8 fast-path rendering."""
    caps: Dict[str, bool] = {
        "h264_nvenc": False,
        "libx264": False,
        "h264_videotoolbox": False,
        "crop": False,
        "scale": False,
        "overlay": False,
        "subtitles": False,
        "ass": False,
        "scale_cuda": False,
        "zoompan": False,
    }
    resolved = ffmpeg_bin if ("/" in ffmpeg_bin or "\\" in ffmpeg_bin) else shutil.which(ffmpeg_bin)
    if not resolved or not shutil.which(resolved):
        return caps

    try:
        enc_res = subprocess.run([resolved, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=5, check=False)
        if enc_res.returncode == 0:
            past_sep = False
            found_encoders = set()
            for line in enc_res.stdout.splitlines():
                s = line.strip()
                if s.startswith("------"):
                    past_sep = True
                    continue
                if past_sep and s:
                    parts = s.split()
                    if len(parts) >= 2 and parts[0].startswith("V"):
                        found_encoders.add(parts[1])
            for enc_name in ("h264_nvenc", "libx264", "h264_videotoolbox"):
                caps[enc_name] = enc_name in found_encoders
    except Exception as e:
        logger.warning(f"Failed to probe ffmpeg encoders for capabilities: {e}")

    try:
        flt_res = subprocess.run([resolved, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=5, check=False)
        if flt_res.returncode == 0:
            found_filters = set()
            for line in flt_res.stdout.splitlines():
                s = line.strip()
                parts = s.split()
                if len(parts) >= 3 and "->" in parts[2]:
                    found_filters.add(parts[1])
            for flt_name in ("crop", "scale", "overlay", "subtitles", "ass", "scale_cuda", "zoompan"):
                caps[flt_name] = flt_name in found_filters
    except Exception as e:
        logger.warning(f"Failed to probe ffmpeg filters for capabilities: {e}")

    return caps


def smoke_test_encoder(encoder: str) -> bool:
    """
    Executes a 0.1s null-muxer test to verify encoder functionality on current hardware/driver.
    Uses 256x256 lavfi synthetic black frame for NVENC minimum-dimension safety without touching disk.
    """
    if not shutil.which("ffmpeg"):
        return False

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:d=0.1",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=5, check=False)
        return res.returncode == 0
    except Exception as e:
        logger.debug(f"Smoke test for encoder '{encoder}' failed with exception: {e}")
        return False


def get_encoder_codec_args(
    encoder: str,
    preset: str = "medium",
    crf: str = "23",
    *,
    quality_mode: Optional[str] = None,
    gpu_index: int = 0,
    profile: str = "high",
) -> List[str]:
    """
    Returns verified FFmpeg CLI flags for the resolved encoder with uniform stream profile.
    """
    enc = (encoder or "libx264").strip().lower()
    qmode = (quality_mode or "").strip().lower() or None

    if enc in ("h264_nvenc", "nvenc"):
        nv_preset = "p3" if qmode == "preview" else "p4"
        nv_cq = str(crf)
        if qmode == "preview" and nv_cq == "23":
            nv_cq = "24"
        elif qmode == "final" and nv_cq == "23":
            nv_cq = "20"
        return [
            "-c:v",
            "h264_nvenc",
            "-gpu",
            str(int(gpu_index)),
            "-preset",
            nv_preset,
            "-profile:v",
            profile,
            "-rc",
            "vbr",
            "-cq",
            nv_cq,
            "-b:v",
            "4500k",
        ]
    elif enc in ("h264_videotoolbox", "videotoolbox"):
        bitrate = "3500k" if qmode == "preview" else "4500k"
        return [
            "-c:v",
            "h264_videotoolbox",
            "-profile:v",
            profile,
            "-b:v",
            bitrate,
            "-prio_speed",
            "1",
        ]
    else:
        # Default libx264 CPU encoding
        x264_preset = preset
        x264_crf = str(crf)
        if qmode == "preview":
            if x264_preset == "medium":
                x264_preset = "veryfast"
            if x264_crf == "23":
                x264_crf = "24"
        elif qmode == "final" and x264_crf == "23":
            x264_crf = "20"
        return [
            "-c:v",
            "libx264",
            "-preset",
            x264_preset,
            "-profile:v",
            profile,
            "-crf",
            x264_crf,
        ]


def resolve_video_encoder(
    requested: Optional[str] = "auto",
    preset: str = "medium",
    crf: str = "23",
) -> Tuple[str, List[str], str]:
    """
    Resolves the best available working encoder with runtime smoke testing.
    Falls back gracefully to libx264 CPU encoding if a requested hardware encoder fails.
    Returns: (resolved_encoder_name, codec_args_list, status_message)
    """
    req = (requested or "auto").strip().lower()
    if req not in SUPPORTED_ENCODERS:
        logger.warning(f"Unrecognized encoder '{requested}'. Defaulting to 'auto'.")
        req = "auto"

    available = probe_available_encoders()

    if req == "auto":
        # 1. Try NVIDIA NVENC (Kaggle/Colab GPU)
        if "h264_nvenc" in available and smoke_test_encoder("h264_nvenc"):
            return (
                "h264_nvenc",
                get_encoder_codec_args("h264_nvenc", preset, crf),
                "Hardware acceleration: NVIDIA NVENC verified",
            )

        # 2. Try Apple VideoToolbox (macOS)
        if "h264_videotoolbox" in available and smoke_test_encoder("h264_videotoolbox"):
            return (
                "h264_videotoolbox",
                get_encoder_codec_args("h264_videotoolbox", preset, crf),
                "Hardware acceleration: Apple VideoToolbox verified",
            )

        # 3. CPU Fallback
        return (
            "libx264",
            get_encoder_codec_args("libx264", preset, crf),
            "Software encoding: libx264 CPU (no working GPU encoder detected)",
        )

    # Explicit request for hardware encoder
    if req in ("h264_nvenc", "h264_videotoolbox"):
        if req in available and smoke_test_encoder(req):
            return (
                req,
                get_encoder_codec_args(req, preset, crf),
                f"Hardware acceleration: {req} verified",
            )
        logger.warning(
            f"Requested encoder '{req}' failed hardware smoke test. Falling back to libx264 CPU."
        )
        return (
            "libx264",
            get_encoder_codec_args("libx264", preset, crf),
            f"Fallback to libx264: requested '{req}' failed hardware smoke test",
        )

    # Explicit libx264 request
    return (
        "libx264",
        get_encoder_codec_args("libx264", preset, crf),
        "Software encoding: libx264 CPU (explicitly requested)",
    )


def resolve_encoder(
    requested_encoder: Optional[str] = "auto",
    preset: str = "medium",
    crf: str = "23",
) -> Tuple[str, List[str], str]:
    """
    Resolves the best available working encoder with runtime smoke testing.
    Convenience alias for resolve_video_encoder.
    Returns: (resolved_encoder_name, codec_args_list, status_message)
    """
    return resolve_video_encoder(requested=requested_encoder, preset=preset, crf=crf)


_videotoolbox_lock = threading.Lock()


def get_hardware_encoder_lock() -> threading.Lock:
    """Returns the shared mutex for serializing Apple VideoToolbox encoding calls."""
    return _videotoolbox_lock


def is_hardware_encoder(encoder: Optional[str]) -> bool:
    """Returns True if the specified encoder name refers to a hardware accelerator."""
    if not encoder:
        return False
    return encoder.strip().lower() in ("h264_nvenc", "h264_videotoolbox", "nvenc", "videotoolbox")


def get_fallback_encoder(encoder: Optional[str] = None) -> str:
    """Returns safe fallback software encoder (libx264)."""
    return "libx264"

