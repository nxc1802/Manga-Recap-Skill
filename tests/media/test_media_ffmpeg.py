"""Tests for unified media FFmpeg/FFprobe execution and duration probing."""

import contextlib
import subprocess
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.errors import DependencyNotFoundError, FFmpegExecutionError
from src.media.ffmpeg import ensure_binary, get_wav_duration, probe_media_duration, run_ffmpeg


def test_ensure_binary_and_run_ffmpeg_timeout_and_lock(tmp_path):
    with patch("shutil.which", return_value=None):
        with pytest.raises(DependencyNotFoundError):
            ensure_binary("ffmpeg")
        with pytest.raises(DependencyNotFoundError):
            run_ffmpeg(["ffmpeg", "-version"])

    entered = []

    @contextlib.contextmanager
    def _track_lock():
        entered.append("enter")
        try:
            yield
        finally:
            entered.append("exit")

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5, stderr=b"partial"),
        ):
            with pytest.raises(FFmpegExecutionError, match="timed out"):
                run_ffmpeg(["ffmpeg", "-i", "a.mp4"], description="Slow job", timeout=5, lock=_track_lock())
    assert entered == ["enter", "exit"]


def test_get_wav_duration_and_probe_media_duration(tmp_path):
    wav_file = tmp_path / "tone.wav"
    with wave.open(str(wav_file), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(b"\x00\x00" * 24000)

    assert abs(get_wav_duration(wav_file) - 0.5) < 1e-6

    with patch("shutil.which", return_value="/usr/bin/ffprobe"):
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout='{"format":{"duration":"12.345"}}', stderr=""),
        ):
            assert probe_media_duration(tmp_path / "out.mp4") == pytest.approx(12.345)

        # Fallback to video stream duration when format.duration is missing
        with patch(
            "subprocess.run",
            return_value=MagicMock(
                returncode=0,
                stdout='{"format":{},"streams":[{"codec_type":"video","duration":"8.25"}]}',
                stderr="",
            ),
        ):
            assert probe_media_duration(tmp_path / "out.mp4") == pytest.approx(8.25)

    with patch("shutil.which", return_value=None):
        assert probe_media_duration(tmp_path / "out.mp4") == 0.0
