"""
src/render/validator.py
FFprobe Stream Validator for Video Engine V2.
Validates video and audio codec, resolution, frame rate, and timeline synchronization.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.core.logger import logger
from src.core.utils import DependencyNotFoundError


class FFprobeValidator:
    """
    Validates rendered MP4 files via FFprobe:
    - Video stream: H.264 / HEVC codec, 1280x720 or 1920x1080 resolution, 30 fps
    - Audio stream: AAC / PCM codec, 48 kHz / 44.1 kHz sample rate
    - Synchronization: Delta between video and audio durations <= 0.10s
    """

    def __init__(
        self,
        allowed_video_codecs: Optional[List[str]] = None,
        allowed_audio_codecs: Optional[List[str]] = None,
        expected_fps: float = 30.0,
        max_duration_delta: float = 0.10,
    ):
        if not shutil.which("ffprobe"):
            raise DependencyNotFoundError("ffprobe binary not found on system PATH.")

        self.allowed_video_codecs = allowed_video_codecs or ["h264", "hevc"]
        self.allowed_audio_codecs = allowed_audio_codecs or ["aac", "pcm_s16le", "mp3"]
        self.expected_fps = expected_fps
        self.max_duration_delta = max_duration_delta

    def probe_media_file(self, media_path: Union[str, Path]) -> Dict[str, Any]:
        """Runs ffprobe on the target media file and parses JSON metadata."""
        path = Path(media_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Media file not found: {path}")

        if not shutil.which("ffprobe"):
            raise DependencyNotFoundError("ffprobe binary not found on PATH.")

        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels,duration",
            "-show_entries", "format=duration,size,format_name",
            "-of", "json",
            str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFprobe execution failed: {res.stderr}")

        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse FFprobe JSON output: {e}\nRaw output: {res.stdout}")

    probe_media = probe_media_file

    def validate_video(
        self,
        video_path: Union[str, Path],
        expected_duration: Optional[float] = None,
        tolerance_seconds: Optional[float] = None,
        expected_width: Optional[int] = None,
        expected_height: Optional[int] = None,
        expected_fps: Optional[float] = None,
        raise_on_error: bool = False,
        strict: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Validates stream integrity and returns a structured validation report.
        Supports both raise_on_error and strict flags for error raising.
        """
        path = Path(video_path).resolve()
        probe_data = self.probe_media_file(path)

        streams = probe_data.get("streams", [])
        fmt = probe_data.get("format", {})

        video_stream: Optional[Dict[str, Any]] = None
        audio_stream: Optional[Dict[str, Any]] = None

        for s in streams:
            ctype = s.get("codec_type")
            if ctype == "video" and video_stream is None:
                video_stream = s
            elif ctype == "audio" and audio_stream is None:
                audio_stream = s

        errors: List[str] = []
        max_delta = tolerance_seconds if tolerance_seconds is not None else self.max_duration_delta

        # 1. Video stream checks
        if video_stream is None:
            errors.append("No video stream found in container.")
        else:
            v_codec = video_stream.get("codec_name", "").lower()
            if v_codec not in self.allowed_video_codecs:
                errors.append(f"Invalid video codec '{v_codec}'; expected one of {self.allowed_video_codecs}.")

            w = int(video_stream.get("width", 0))
            h = int(video_stream.get("height", 0))

            if expected_width is not None and w != expected_width:
                errors.append(f"Video width mismatch: {w} != expected {expected_width}")
            if expected_height is not None and h != expected_height:
                errors.append(f"Video height mismatch: {h} != expected {expected_height}")

            if expected_width is None and expected_height is None:
                if (w, h) not in [(1280, 720), (1920, 1080), (720, 1280), (1080, 1920)]:
                    if w < 640 or h < 360:
                        errors.append(f"Sub-standard resolution {w}x{h}; minimum 640x360 required.")

            # Frame rate check
            fps_str = video_stream.get("r_frame_rate", "0/1")
            target_fps = expected_fps if expected_fps is not None else self.expected_fps
            try:
                num, den = fps_str.split("/")
                fps_val = float(num) / float(den) if float(den) != 0 else 0.0
                if abs(fps_val - target_fps) > 1.0:
                    errors.append(f"Video frame rate {fps_val:.2f} deviates from expected {target_fps} fps.")
            except Exception as e:
                errors.append(f"Failed to parse video frame rate '{fps_str}': {e}")

        # 2. Audio stream checks
        if audio_stream is None:
            errors.append("No audio stream found in container.")
        else:
            a_codec = audio_stream.get("codec_name", "").lower()
            if a_codec not in self.allowed_audio_codecs:
                errors.append(f"Invalid audio codec '{a_codec}'; expected one of {self.allowed_audio_codecs}.")

            sr = int(audio_stream.get("sample_rate", 0))
            if sr < 44100:
                errors.append(f"Audio sample rate {sr} Hz is below acceptable 44100 Hz minimum.")

        # 3. Duration synchronization check
        format_duration = float(fmt.get("duration", 0.0))
        video_dur = float(video_stream.get("duration", format_duration)) if video_stream else format_duration
        audio_dur = float(audio_stream.get("duration", format_duration)) if audio_stream else format_duration

        stream_delta = abs(video_dur - audio_dur)
        if stream_delta > max_delta:
            errors.append(
                f"Audio/video stream duration mismatch: video={video_dur:.3f}s, audio={audio_dur:.3f}s "
                f"(delta={stream_delta:.3f}s > {max_delta:.3f}s)."
            )

        duration_diff = stream_delta
        if expected_duration is not None and expected_duration > 0:
            exp_delta = abs(format_duration - expected_duration)
            duration_diff = exp_delta
            if exp_delta > max_delta:
                errors.append(
                    f"Duration sync mismatch: overall duration {format_duration:.3f}s differs from expected {expected_duration:.3f}s "
                    f"by {exp_delta:.3f}s (tolerance={max_delta:.3f}s)."
                )

        valid = len(errors) == 0

        report = {
            "valid": valid,
            "file_path": str(path),
            "format": fmt.get("format_name", ""),
            "duration_seconds": round(format_duration, 4),
            "total_duration_seconds": round(format_duration, 4),
            "duration_diff_seconds": round(duration_diff, 4),
            "stream_duration_delta_seconds": round(stream_delta, 4),
            "video": {
                "codec": video_stream.get("codec_name", "") if video_stream else None,
                "width": int(video_stream.get("width", 0)) if video_stream else None,
                "height": int(video_stream.get("height", 0)) if video_stream else None,
                "fps": round(fps_val, 2) if (video_stream and 'fps_val' in locals()) else None,
                "duration": round(video_dur, 4) if video_stream else None,
            },
            "audio": {
                "codec": audio_stream.get("codec_name", "") if audio_stream else None,
                "sample_rate": int(audio_stream.get("sample_rate", 0)) if audio_stream else None,
                "channels": int(audio_stream.get("channels", 0)) if audio_stream else None,
                "duration": round(audio_dur, 4) if audio_stream else None,
            },
            "errors": errors,
        }

        should_raise = raise_on_error or (strict is True)
        if should_raise and not valid:
            raise ValueError(f"FFprobe validation failed for {path.name}: " + "; ".join(errors))

        return report


# Alias for backward-compatibility with plan and previous references
StreamValidator = FFprobeValidator
