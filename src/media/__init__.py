"""Unified media processing and FFmpeg execution boundary."""

from src.media.ffmpeg import (
    ensure_binary,
    get_wav_duration,
    probe_media_duration,
    run_ffmpeg,
)

__all__ = [
    "ensure_binary",
    "get_wav_duration",
    "probe_media_duration",
    "run_ffmpeg",
]
