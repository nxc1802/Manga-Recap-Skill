"""Shared FFmpeg/FFprobe binary checks, process execution, and media duration helpers."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, ContextManager, List, Optional

from src.core.errors import DependencyNotFoundError, FFmpegExecutionError


def ensure_binary(binary: str = "ffmpeg") -> str:
    """Verifies ``binary`` exists on PATH and returns its resolved executable path."""
    resolved = shutil.which(binary)
    if not resolved:
        raise DependencyNotFoundError(
            f"Binary '{binary}' not found on system PATH. Please install FFmpeg."
        )
    return resolved


def get_wav_duration(path: Path) -> float:
    """Measures uncompressed WAV file duration from RIFF header in sub-millisecond time."""
    target = Path(path)
    try:
        with wave.open(str(target), "rb") as wav:
            if wav.getnframes() > 0 and wav.getframerate() > 0:
                return wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"Invalid WAV header: {target}") from exc
    raise ValueError(f"WAV contains no frames: {target}")


def run_ffmpeg(
    cmd: List[str],
    description: str = "FFmpeg process",
    *,
    cwd: Optional[Path] = None,
    timeout: Optional[int] = None,
    input_bytes: Optional[bytes] = None,
    lock: Optional[ContextManager[Any]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """
    Executes an FFmpeg/FFprobe subprocess with binary presence verification,
    optional hardware lock, timeout handling, and structured stderr error reporting.
    """
    binary = cmd[0] if cmd else "ffmpeg"
    ensure_binary(binary)

    run_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": input_bytes is None,
    }
    if cwd is not None:
        run_kwargs["cwd"] = cwd
    if timeout is not None:
        run_kwargs["timeout"] = timeout
    if input_bytes is not None:
        run_kwargs["input"] = input_bytes

    ctx = lock if lock is not None else contextlib.nullcontext()
    try:
        with ctx:
            result = subprocess.run(cmd, **run_kwargs)
    except FileNotFoundError as exc:
        raise DependencyNotFoundError(
            f"Binary '{binary}' not found on system PATH. Please install FFmpeg."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        stderr_raw = getattr(exc, "stderr", b"") or b""
        stderr_str = (
            stderr_raw.decode(errors="replace")
            if isinstance(stderr_raw, (bytes, bytearray))
            else str(stderr_raw)
        )
        raise FFmpegExecutionError(f"{description} timed out", 124, stderr_str) from exc

    if check and result.returncode != 0:
        stderr_raw = getattr(result, "stderr", "")
        stderr_str = (
            stderr_raw.decode(errors="replace")
            if isinstance(stderr_raw, (bytes, bytearray))
            else str(stderr_raw or "")
        )
        raise FFmpegExecutionError(f"{description} failed", result.returncode, stderr_str)

    return result


def probe_media_duration(media_path: Path, *, timeout: int = 15) -> float:
    """Queries duration in seconds for a media container using ``ffprobe`` (0.0 if unavailable)."""
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        res = run_ffmpeg(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(media_path),
            ],
            description="FFprobe media duration check",
            timeout=timeout,
            check=True,
        )
        probe_data = json.loads(res.stdout or "{}")
        fmt_dur = probe_data.get("format", {}).get("duration")
        if fmt_dur is not None and float(fmt_dur) > 0:
            return float(fmt_dur)
        for stream in probe_data.get("streams", []):
            if stream.get("codec_type") == "video" and stream.get("duration") is not None:
                dur = float(stream["duration"])
                if dur > 0:
                    return dur
    except Exception:
        return 0.0
    return 0.0
