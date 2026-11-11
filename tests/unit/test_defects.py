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

from src.core.utils import (
    DependencyNotFoundError,
    FFmpegExecutionError,
    cleanup_intermediate_files,
    get_next_video_path,
    resolve_video_output_path,
    run_ffmpeg,
)
from src.pipeline import pipeline, MangaRecapPipeline


class TestDefect1CleanupTempFiles:
    """Verifies that phantom cleanup_temp_files() is resolved and safely removes temp assets."""

    def test_pipeline_has_cleanup_temp_files_method(self):
        assert hasattr(pipeline, "cleanup_temp_files"), (
            "MangaRecapPipeline must implement cleanup_temp_files() to prevent phantom attribute errors"
        )
        assert callable(pipeline.cleanup_temp_files)

    def test_cleanup_temp_files_execution(self, tmp_path, monkeypatch):
        # Configure storage dir to tmp_path
        from src.core.config import config
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(config, "storage_dir", tmp_path / "data" / "projects")

        proj_dir = tmp_path / "data" / "projects" / "test_proj"
        chap_dir = proj_dir / "chapters" / "1"
        chap_dir.mkdir(parents=True, exist_ok=True)
        video_chap_dir = proj_dir / "video" / "chapters"
        video_chap_dir.mkdir(parents=True, exist_ok=True)

        # Create temporary scratch files
        raw_temp = video_chap_dir / "chapter_1_raw_temp.mp4"
        subs_temp = video_chap_dir / "chapter_1_subs_temp.srt"
        concat_temp = proj_dir / "compile_concat_list.txt"
        final_video = video_chap_dir / "chapter_1_final_v1.mp4"

        raw_temp.write_text("temp video")
        subs_temp.write_text("temp subs")
        concat_temp.write_text("temp concat")
        final_video.write_text("final video")

        cleaned = pipeline.cleanup_temp_files(chapter="1", suffix="final", project="test_proj")
        assert not raw_temp.exists(), "chapter_1_raw_temp.mp4 should be cleaned up"
        assert not subs_temp.exists(), "chapter_1_subs_temp.srt should be cleaned up"
        assert final_video.exists(), "Final video file must be preserved"

    def test_cleanup_intermediate_files_honors_keep_temp(self, tmp_path):
        temp_file = tmp_path / "test_raw_temp.mp4"
        temp_file.write_text("dummy")

        # When keep_temp is True, files must not be deleted
        cleaned = cleanup_intermediate_files(tmp_path, keep_temp=True)
        assert len(cleaned) == 0
        assert temp_file.exists()

        # When keep_temp is False, files must be removed
        cleaned = cleanup_intermediate_files(tmp_path, keep_temp=False)
        assert len(cleaned) == 1
        assert not temp_file.exists()

    def test_cleanup_temp_files_accepts_keep_temp_argument(self, tmp_path, monkeypatch):
        from src.core.config import config
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(config, "storage_dir", tmp_path / "data" / "projects")

        proj_dir = tmp_path / "data" / "projects" / "test_proj"
        chap_dir = proj_dir / "chapters" / "1"
        chap_dir.mkdir(parents=True, exist_ok=True)
        raw_temp = chap_dir / "chapter_1_raw_temp.mp4"
        raw_temp.write_text("temp video")

        # When keep_temp is True, files must not be deleted
        cleaned_kept = pipeline.cleanup_temp_files(chapter=1, project="test_proj", keep_temp=True)
        assert len(cleaned_kept) == 0
        assert raw_temp.exists()

        # When keep_temp is False, files must be deleted
        cleaned_wiped = pipeline.cleanup_temp_files(chapter=1, project="test_proj", keep_temp=False)
        assert len(cleaned_wiped) == 1
        assert not raw_temp.exists()

    def test_cleanup_intermediate_files_removes_generic_tmp(self, tmp_path):
        generic_tmp = tmp_path / "work.tmp"
        dot_tmp = tmp_path / ".session.tmp"
        regular_file = tmp_path / "keep.mp4"

        generic_tmp.write_text("tmp1")
        dot_tmp.write_text("tmp2")
        regular_file.write_text("keep")

        cleaned = cleanup_intermediate_files(tmp_path, keep_temp=False)
        assert generic_tmp.name in cleaned
        assert dot_tmp.name in cleaned
        assert not generic_tmp.exists()
        assert not dot_tmp.exists()
        assert regular_file.exists()


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


class TestDefect3OutputPathVersioningAndSuffix:
    """Verifies output path resolution with suffix and auto-incrementing version."""

    def test_get_next_video_path_default_no_suffix(self, tmp_path):
        p1 = get_next_video_path(tmp_path, "1")
        assert p1.name == "chapter_1_1.mp4"
        p1.write_text("video 1")

        p2 = get_next_video_path(tmp_path, "1")
        assert p2.name == "chapter_1_2.mp4"

    def test_get_next_video_path_with_suffix(self, tmp_path):
        p_preview_1 = get_next_video_path(tmp_path, 2, suffix="preview")
        assert p_preview_1.name == "chapter_2_preview_v1.mp4"
        p_preview_1.write_text("preview 1")

        p_preview_2 = get_next_video_path(tmp_path, 2, suffix="preview")
        assert p_preview_2.name == "chapter_2_preview_v2.mp4"

        # Independent suffix 'final' starts at v1
        p_final_1 = get_next_video_path(tmp_path, 2, suffix="final")
        assert p_final_1.name == "chapter_2_final_v1.mp4"

    def test_resolve_video_output_path(self, tmp_path):
        path_v1 = resolve_video_output_path(tmp_path, "3", suffix="agent", version=1)
        assert path_v1.name == "chapter_3_agent_v1.mp4"

        path_auto = resolve_video_output_path(tmp_path, "3", suffix="agent")
        assert path_auto.name == "chapter_3_agent_v1.mp4"
        path_auto.write_text("content")

        path_auto_2 = resolve_video_output_path(tmp_path, "3", suffix="agent")
        assert path_auto_2.name == "chapter_3_agent_v2.mp4"


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
