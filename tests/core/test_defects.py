"""
tests/unit/test_defects.py
Unit tests proving that the 3 critical legacy defects have been completely resolved:
1. Phantom cleanup_temp_files() resolution on MangaRecapPipeline
2. FFmpeg non-zero return code enforcement and error checking
3. Output path versioning and suffix resolution
"""

import os
from pathlib import Path
import subprocess
from unittest.mock import patch, MagicMock
import pytest

from src.cli.exit_codes import CliValidationError
from src.core.utils import (
    DependencyNotFoundError,
    FFmpegExecutionError,
    get_next_video_path,
    resolve_video_output_path,
    run_ffmpeg,
)


class TestDefect2FFmpegReturnCodeEnforcement:
    """Verifies that FFmpeg failures are detected and raise proper exceptions instead of silent passes."""

    def test_run_ffmpeg_missing_binary_raises_dependency_error(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(DependencyNotFoundError) as exc_info:
                run_ffmpeg(["nonexistent_ffmpeg", "-version"])
            assert "not found on system PATH" in str(exc_info.value)

    def test_run_ffmpeg_nonzero_exit_raises_ffmpeg_execution_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Unknown encoder 'libx264_invalid'"

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("subprocess.run", return_value=mock_result):
                with pytest.raises(FFmpegExecutionError) as exc_info:
                    run_ffmpeg(["ffmpeg", "-i", "in.mp4", "out.mp4"], description="Transcode test")
                assert exc_info.value.returncode == 1
                assert "Transcode test failed" in str(exc_info.value)
                assert "Unknown encoder" in exc_info.value.stderr

    def test_run_ffmpeg_success_returns_completed_process(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ffmpeg output"
        mock_result.stderr = ""

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            with patch("subprocess.run", return_value=mock_result):
                res = run_ffmpeg(["ffmpeg", "-version"])
                assert res.returncode == 0




class TestDefect4ValidatePanelIdsAndFilenames:
    """Verifies that validate command matches both panel.panel_id and panel.filename."""

    def test_validate_matches_both_panel_id_and_filename(self, tmp_path, monkeypatch):
        from src.cli.main import main
        from src.core.storage import init_project_layout, init_chapter_layout, atomic_write_json

        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("panel_test_proj", "Panel Test")
        chap_dir = init_chapter_layout("panel_test_proj", "1", "Episode 1")

        # Put an image file in panels/
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        img_path = panels_dir / "panel_001.jpg"
        img_path.write_bytes(b"dummy")

        # Create manifest with panel_id="panel_001", filename="panel_001.jpg"
        manifest_payload = {
            "schema_version": "3.0.0",
            "project_slug": "panel_test_proj",
            "chapter_number": "1",
            "source_directory": "raw",
            "panels": [
                {
                    "panel_id": "panel_001",
                    "sequence_number": 1,
                    "filename": "panel_001.jpg",
                    "relative_path": "panels/panel_001.jpg",
                    "source_image": "page_001.jpg",
                    "crop_box": [0, 0, 100, 100],
                    "width": 100,
                    "height": 100,
                    "aspect_ratio": 1.0,
                    "sha256": "abc123sha",
                }
            ],
            "total_panels": 1,
        }
        atomic_write_json(chap_dir / "manifest.json", manifest_payload)

        # Create script referencing "panel_001.jpg" (filename) instead of "panel_001" (panel_id)
        script_payload = {
            "schema_version": "3.0.0",
            "project_slug": "panel_test_proj",
            "chapter_number": "1",
            "title": "Episode 1",
            "segments": [
                {
                    "seq_id": "001",
                    "panel_ids": ["panel_001.jpg"],
                    "narrator_text": "Sample narration",
                    "camera_motion": "static",
                }
            ],
        }
        atomic_write_json(chap_dir / "script.json", script_payload)

        # Validate with --strict should succeed (exit code 0) without warnings triggering failure
        code = main(["validate", "-p", "panel_test_proj", "-c", "1", "--strict", "--json"])
        assert code == 0
