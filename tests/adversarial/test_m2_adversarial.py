"""
tests/adversarial/test_m2_adversarial.py
Adversarial empirical stress tests and edge-case verification for Milestone M2 (PR-02 & PR-03).
Covers:
1. Defect 1 Stress: pipeline.cleanup_temp_files & cleanup_intermediate_files under hostile/edge conditions.
2. Defect 2 Stress: run_ffmpeg error handling, exit codes matrix, and CLI envelope propagation.
3. Defect 3 Stress: get_next_video_path & resolve_video_output_path versioning under heavy interleaving and anomalies.
"""

from __future__ import annotations

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
from src.pipeline import MangaRecapPipeline, pipeline


# ==============================================================================
# 1. Defect 1 Stress: Cleanup of Temporary & Intermediate Files
# ==============================================================================

class TestDefect1CleanupTempFilesAdversarial:
    """Stress tests and boundary condition exploration for temp file cleanup."""

    def test_cleanup_temp_files_removes_known_intermediates_preserves_finals(self, tmp_path, monkeypatch):
        """Verifies intermediate scratch files are wiped while raw panels, clips, and finals survive."""
        monkeypatch.setattr(config, "storage_dir", tmp_path / "projects")

        proj_name = "adversarial_proj"
        proj_dir = tmp_path / "projects" / proj_name
        chap_dir = proj_dir / "chapters" / "1"
        video_chap_dir = proj_dir / "video" / "chapters"
        panels_dir = chap_dir / "panels"
        audio_dir = chap_dir / "audio" / "clips"

        for d in [chap_dir, video_chap_dir, panels_dir, audio_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Essential assets (MUST NOT BE DELETED)
        final_video = video_chap_dir / "chapter_1_final_v1.mp4"
        final_video_2 = chap_dir / "video" / "chapter_1_preview_v2.mp4"
        raw_panel = panels_dir / "panel_001.jpg"
        audio_clip = audio_dir / "clip_001.wav"
        chapter_meta = chap_dir / "chapter.json"

        # Intermediate files (MUST BE DELETED)
        raw_temp = video_chap_dir / "chapter_1_raw_temp.mp4"
        raw_temp_chap = chap_dir / "chapter_1_raw_temp_segment.mp4"
        subs_temp = video_chap_dir / "chapter_1_subs_temp.srt"
        subs_temp_chap = chap_dir / "chapter_1_subs_temp.srt"
        concat_txt = chap_dir / "compile_concat_list.txt"
        dot_tmp_concat = chap_dir / ".tmp_concat_files.txt"

        survivors = [final_video, final_video_2, raw_panel, audio_clip, chapter_meta]
        targets = [raw_temp, raw_temp_chap, subs_temp, subs_temp_chap, concat_txt, dot_tmp_concat]

        for f in survivors + targets:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("payload")

        cleaned = pipeline.cleanup_temp_files(chapter="1", project=proj_name)

        # Assert all targets were unlinked
        for t in targets:
            assert not t.exists(), f"Expected {t.name} to be unlinked, but still exists"

        # Assert all survivors remain untouched
        for s in survivors:
            assert s.exists(), f"Asset {s.name} was erroneously deleted"

        assert len(cleaned) == len(targets)

    def test_pipeline_cleanup_temp_files_keep_temp_signature_limitation(self):
        """
        Empirically verifies whether pipeline.cleanup_temp_files accepts keep_temp argument.
        Remediated: keep_temp: bool = False is part of the method signature and does not raise TypeError.
        """
        cleaned = pipeline.cleanup_temp_files(chapter="1", keep_temp=True)
        assert isinstance(cleaned, list)

    def test_cleanup_intermediate_files_generic_tmp_pattern_boundary(self, tmp_path):
        """
        Tests behavior on arbitrary '.tmp' files vs designed patterns.
        Remediated: cleanup_intermediate_files now specifies patterns including generic *.tmp,
        so generic .tmp scratch files are unlinked when keep_temp=False.
        """
        designed_temp = tmp_path / "chapter_1_raw_temp.mp4"
        designed_concat = tmp_path / ".tmp_concat_list.txt"
        generic_tmp = tmp_path / "scratch_work.tmp"
        dot_tmp = tmp_path / ".tmp_session_12345.tmp"

        for f in [designed_temp, designed_concat, generic_tmp, dot_tmp]:
            f.write_text("scratch")

        cleaned = cleanup_intermediate_files(tmp_path, keep_temp=False)

        assert not designed_temp.exists()
        assert not designed_concat.exists()
        assert not generic_tmp.exists(), "Generic .tmp is unlinked by *.tmp pattern"
        assert not dot_tmp.exists(), "Generic dot-tmp is unlinked by *.tmp pattern"
        assert set(cleaned) == {designed_temp.name, designed_concat.name, generic_tmp.name, dot_tmp.name}

    def test_cleanup_temp_files_nonexistent_directory_safety(self, tmp_path, monkeypatch):
        """Verifies calling cleanup on nonexistent chapter or project does not raise unhandled errors."""
        monkeypatch.setattr(config, "storage_dir", tmp_path / "projects")
        cleaned = pipeline.cleanup_temp_files(chapter="99999", project="ghost_project")
        assert cleaned == []

    def test_cleanup_temp_files_permission_error_graceful_recovery(self, tmp_path, monkeypatch):
        """Verifies that an unlink error on one file does not halt cleanup of remaining files."""
        monkeypatch.setattr(config, "storage_dir", tmp_path / "projects")

        proj_dir = tmp_path / "projects" / "p_perm"
        chap_dir = proj_dir / "chapters" / "1"
        chap_dir.mkdir(parents=True, exist_ok=True)

        temp1 = chap_dir / "clip_1_raw_temp.mp4"
        temp2 = chap_dir / "clip_2_raw_temp.mp4"
        temp1.write_text("t1")
        temp2.write_text("t2")

        original_unlink = Path.unlink

        def mock_unlink(self, *args, **kwargs):
            if self.name == "clip_1_raw_temp.mp4":
                raise PermissionError("Access denied")
            return original_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", mock_unlink):
            cleaned = pipeline.cleanup_temp_files(chapter="1", project="p_perm")

        # clip_2 was cleaned despite clip_1 failing
        assert "clip_2_raw_temp.mp4" in cleaned
        assert not temp2.exists()
        assert temp1.exists()


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

    def test_cli_compile_missing_ffmpeg_yields_dependency_exit_code_4(self, tmp_path, monkeypatch):
        """Verifies manga-recap compile emits exit code 4 and DEPENDENCY_ERROR envelope when FFmpeg missing."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(config, "storage_dir", tmp_path / "projects")

        init_project_layout("adv_p", "Adv Project")
        init_chapter_layout("adv_p", "1")

        with patch("shutil.which", return_value=None):
            with patch.object(sys, "argv", ["manga-recap", "compile", "-p", "adv_p", "--chapters", "1", "--json"]):
                out_buf = io.StringIO()
                with patch("sys.stdout", out_buf):
                    code = main()

                assert code == ExitCode.DEPENDENCY_ERROR
                assert code == 4

                payload = json.loads(out_buf.getvalue())
                assert payload["exit_code"] == 4
                assert payload["status"] == "error"
                assert payload["error"]["code"] == "DEPENDENCY_ERROR"

    def test_cli_compile_ffmpeg_failure_yields_general_error_exit_code_1(self, tmp_path, monkeypatch):
        """Verifies manga-recap compile emits exit code 1 and captures stderr when FFmpeg transcode fails."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(config, "storage_dir", tmp_path / "projects")

        init_project_layout("adv_p2", "Adv Project 2")
        init_chapter_layout("adv_p2", "1")
        chap_dir = tmp_path / "projects" / "adv_p2" / "chapters" / "1"
        (chap_dir / "video").mkdir(parents=True, exist_ok=True)
        (chap_dir / "video" / "chapter_1_final_v1.mp4").write_text("dummy")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "FFmpeg codec crash: out of memory / corrupted bitstream"
        mock_result.stdout = ""

        with patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            with patch("subprocess.run", return_value=mock_result):
                with patch.object(sys, "argv", ["manga-recap", "compile", "-p", "adv_p2", "--chapters", "1", "--approved", "--json"]):
                    out_buf = io.StringIO()
                    with patch("sys.stdout", out_buf):
                        code = main()

                    assert code == ExitCode.GENERAL_ERROR
                    assert code == 1

                    payload = json.loads(out_buf.getvalue())
                    assert payload["exit_code"] == 1
                    assert payload["status"] == "error"
                    assert payload["error"]["code"] == "GENERAL_ERROR"
                    assert "FFmpeg codec crash" in payload["error"]["message"]


# ==============================================================================
# 3. Defect 3 Stress: Video Output Path Versioning Collisions
# ==============================================================================

class TestDefect3VideoVersioningAdversarial:
    """Stress tests for video path versioning, suffix isolation, and edge formats."""

    def test_consecutive_multi_suffix_interleaved_heavy_stress(self, tmp_path):
        """
        Stress test: performs 100 interleaved requests across 4 suffixes:
        'preview', 'final', 'agent', and default (None).
        Verifies absolute uniqueness of filenames and strictly increasing versions.
        """
        suffixes = ["preview", "final", "agent", None]
        seen_paths = set()
        version_tracker = {s: 0 for s in suffixes}

        for cycle in range(25):  # 25 cycles * 4 suffixes = 100 files
            for s in suffixes:
                p = get_next_video_path(tmp_path, chapter=1, suffix=s)

                assert p not in seen_paths, f"COLLISION: Path {p.name} was returned twice!"
                seen_paths.add(p)

                # Determine expected filename
                version_tracker[s] += 1
                ver = version_tracker[s]
                if s is not None:
                    assert p.name == f"chapter_1_{s}_v{ver}.mp4"
                else:
                    assert p.name == f"chapter_1_{ver}.mp4"

                # Touch file to simulate creation
                p.write_text(f"dummy video cycle {cycle}")

    def test_version_gaps_resilience(self, tmp_path):
        """Verifies that non-sequential version gaps (e.g., v1, v4, v15) advance to max+1."""
        video_dir = tmp_path / "video" / "chapters"
        video_dir.mkdir(parents=True, exist_ok=True)

        (video_dir / "chapter_5_preview_v1.mp4").write_text("v1")
        (video_dir / "chapter_5_preview_v4.mp4").write_text("v4")
        (video_dir / "chapter_5_preview_v15.mp4").write_text("v15")

        next_path = get_next_video_path(tmp_path, chapter=5, suffix="preview")
        assert next_path.name == "chapter_5_preview_v16.mp4"

    def test_legacy_format_detection_in_get_next_video_path(self, tmp_path):
        """
        Verifies get_next_video_path detects legacy unversioned and underscore-versioned files:
        e.g. chapter_3_preview.mp4 (treated as v1) and chapter_3_preview_5.mp4 (treated as v5).
        """
        video_dir = tmp_path / "video" / "chapters"
        video_dir.mkdir(parents=True, exist_ok=True)

        (video_dir / "chapter_3_preview.mp4").write_text("legacy unversioned")
        p = get_next_video_path(tmp_path, chapter=3, suffix="preview")
        assert p.name == "chapter_3_preview_v2.mp4"

        (video_dir / "chapter_3_preview_5.mp4").write_text("legacy underscore")
        p2 = get_next_video_path(tmp_path, chapter=3, suffix="preview")
        assert p2.name == "chapter_3_preview_v6.mp4"

    def test_resolve_video_output_path_auto_increment_and_explicit(self, tmp_path):
        """Tests resolve_video_output_path across explicit version and auto-increment."""
        chap_dir = tmp_path / "chapters" / "2"
        chap_dir.mkdir(parents=True, exist_ok=True)

        # 1. Explicit versions
        exp1 = resolve_video_output_path(chap_dir, chapter_num=2, suffix="agent", version=10)
        assert exp1.name == "chapter_2_agent_v10.mp4"

        # 2. Auto-increment on empty directory
        auto1 = resolve_video_output_path(chap_dir, chapter_num=2, suffix="agent")
        assert auto1.name == "chapter_2_agent_v1.mp4"
        auto1.write_text("content v1")

        auto2 = resolve_video_output_path(chap_dir, chapter_num=2, suffix="agent")
        assert auto2.name == "chapter_2_agent_v2.mp4"
        auto2.write_text("content v2")

        # Independent suffix 'preview' starts at v1
        auto_prev = resolve_video_output_path(chap_dir, chapter_num=2, suffix="preview")
        assert auto_prev.name == "chapter_2_preview_v1.mp4"

    def test_multiple_chapters_isolated_in_same_video_dir(self, tmp_path):
        """Verifies chapter 1 and chapter 2 versions do not interfere in a shared video directory."""
        video_dir = tmp_path / "video" / "chapters"
        video_dir.mkdir(parents=True, exist_ok=True)

        (video_dir / "chapter_1_final_v50.mp4").write_text("c1")

        p_ch2 = get_next_video_path(tmp_path, chapter=2, suffix="final")
        assert p_ch2.name == "chapter_2_final_v1.mp4"

        p_ch1 = get_next_video_path(tmp_path, chapter=1, suffix="final")
        assert p_ch1.name == "chapter_1_final_v51.mp4"
