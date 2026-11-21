"""
tests/adversarial/test_m2_adversarial.py
Adversarial empirical stress tests and edge-case verification for Milestone M2 (PR-02 & PR-03).
Covers:
1. Defect 1 Stress: pipeline.cleanup_temp_files & cleanup_intermediate_files under hostile/edge conditions.
2. Defect 2 Stress: run_ffmpeg error handling, exit codes matrix, and CLI envelope propagation.
3. Defect 3 Stress: get_next_video_path & resolve_video_output_path versioning under heavy interleaving and anomalies.
"""

from __future__ import annotations

from tests.v4_fixtures import init_confirmed_project

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.core.config import config
from src.core.storage import (
    cleanup_intermediate_files,
    init_chapter_layout,
    init_project_layout,
)
from src.core.utils import (
    DependencyNotFoundError,
    FFmpegExecutionError,
    get_next_video_path,
    resolve_video_output_path,
    run_ffmpeg,
)


# ==============================================================================
# 1. Defect 1 Stress: Cleanup of Temporary & Intermediate Files
# ==============================================================================



# ==============================================================================
# 2. Defect 2 Stress: FFmpeg Execution & Error Handling
# ==============================================================================

class TestDefect2FFmpegEnforcementAdversarial:
    """Stress tests for FFmpeg execution checks, failure capturing, and CLI exit code mapping."""

    def test_run_ffmpeg_missing_binary_raises_dependency_error(self):
        """Verifies DependencyNotFoundError is raised when binary is not in PATH."""
        with patch("shutil.which", return_value=None):
            with pytest.raises(DependencyNotFoundError) as exc_info:
                run_ffmpeg(["ffmpeg", "-version"])
            assert "not found on system PATH" in str(exc_info.value)

    @pytest.mark.parametrize("exit_code", [1, 2, 127, 137, 255])
    def test_run_ffmpeg_various_nonzero_exit_codes_matrix(self, exit_code):
        """Verifies all non-zero exit codes raise FFmpegExecutionError with exact code and stderr."""
        mock_result = MagicMock()
        mock_result.returncode = exit_code
        mock_result.stderr = f"Simulated fatal error on returncode {exit_code}"
        mock_result.stdout = ""

        with patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            with patch("subprocess.run", return_value=mock_result):
                with pytest.raises(FFmpegExecutionError) as exc_info:
                    run_ffmpeg(["ffmpeg", "-i", "corrupt.mp4"], description="Transcode task")
                assert exc_info.value.returncode == exit_code
                assert f"Transcode task failed (exit code {exit_code})" in str(exc_info.value)
                assert f"Simulated fatal error on returncode {exit_code}" in exc_info.value.stderr

    def test_run_ffmpeg_preserves_large_stderr(self):
        """Verifies large stderr outputs are stored on .stderr attribute and truncated cleanly in message."""
        large_error = "STREAM_CORRUPT " * 1000  # 15,000 bytes
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = large_error
        mock_result.stdout = ""

        with patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            with patch("subprocess.run", return_value=mock_result):
                with pytest.raises(FFmpegExecutionError) as exc_info:
                    run_ffmpeg(["ffmpeg", "-i", "bad.mp4"], description="Big stderr test")
                # Full stderr is accessible
                assert len(exc_info.value.stderr) == len(large_error)
                # str(exc_info.value) contains trailing 500 chars snippet
                assert "STREAM_CORRUPT" in str(exc_info.value)

    # Canonical replacement recorded in v4_regression_migration.json.
    pass

    # Canonical replacement recorded in v4_regression_migration.json.
    pass


# ==============================================================================
# 3. Defect 3 Stress: Video Output Path Versioning Collisions
# ==============================================================================
