"""
tests/unit/test_video_validator.py
Unit test suite for StreamValidator and ffprobe media inspection (PR-07).
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from src.core.utils import DependencyNotFoundError
from src.render.validator import StreamValidator


def create_synthetic_mp4(out_path: Path, duration_sec: float = 2.0, width: int = 1280, height: int = 720, fps: int = 30) -> Path:
    """Generates a real minimal MP4 file with H.264 video and AAC audio using FFmpeg."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=navy:s={width}x{height}:r={fps}:d={duration_sec}",
        "-f", "lavfi", "-i", f"sine=frequency=440:r=48000:d={duration_sec}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        str(out_path)
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out_path


class TestStreamValidator:
    """Tests for StreamValidator."""

    def setup_method(self):
        self.validator = StreamValidator()

    def test_init_raises_when_ffprobe_missing(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(DependencyNotFoundError):
                StreamValidator()

    def test_probe_media_missing_file_raises_error(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist.mp4"
        with pytest.raises(FileNotFoundError):
            self.validator.probe_media(nonexistent)

    def test_validate_video_valid_synthetic_stream(self, tmp_path):
        mp4_path = create_synthetic_mp4(tmp_path / "test_valid.mp4", duration_sec=2.0)
        res = self.validator.validate_video(
            video_path=mp4_path,
            expected_duration=2.0,
            tolerance_seconds=0.15,
            expected_width=1280,
            expected_height=720,
            expected_fps=30.0,
        )

        assert res["valid"] is True
        assert res["video"]["codec"] in ("h264", "avc1")
        assert res["video"]["width"] == 1280
        assert res["video"]["height"] == 720
        assert pytest.approx(res["video"]["fps"], 0.5) == 30.0
        assert res["audio"]["codec"] == "aac"
        assert res["audio"]["sample_rate"] == 48000
        assert res["duration_diff_seconds"] <= 0.15
        assert len(res["errors"]) == 0

    def test_validate_video_detects_duration_mismatch(self, tmp_path):
        mp4_path = create_synthetic_mp4(tmp_path / "test_mismatch.mp4", duration_sec=1.5)
        # Expecting 5.0 seconds but actual is 1.5 seconds
        res = self.validator.validate_video(
            video_path=mp4_path,
            expected_duration=5.0,
            tolerance_seconds=0.15,
        )
        assert res["valid"] is False
        assert any("Duration sync mismatch" in e for e in res["errors"])

    def test_validate_video_detects_resolution_mismatch(self, tmp_path):
        mp4_path = create_synthetic_mp4(tmp_path / "test_res.mp4", duration_sec=1.0, width=640, height=360)
        res = self.validator.validate_video(
            video_path=mp4_path,
            expected_duration=1.0,
            expected_width=1280,
            expected_height=720,
        )
        assert res["valid"] is False
        assert any("width mismatch" in e for e in res["errors"])

    def test_validate_video_raise_on_error(self, tmp_path):
        mp4_path = create_synthetic_mp4(tmp_path / "test_raise.mp4", duration_sec=1.0)
        with pytest.raises(ValueError):
            self.validator.validate_video(
                video_path=mp4_path,
                expected_duration=10.0,
                tolerance_seconds=0.1,
                raise_on_error=True,
            )
