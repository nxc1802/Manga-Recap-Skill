"""
tests/unit/test_phase4_workspace_export.py
Comprehensive unit tests for Phase 4: Local Workspace & Export.

Covers:
1. Standardized output directory and export file naming (padded and unpadded).
2. Retention of intermediate data during processing and cache hits.
3. Safe cleanup behavior: successful cleanup of temporary files when output is verified.
4. Cleanup refusal / error when output video is missing, corrupted, or below minimum size threshold.
5. CLI integration with --cleanup in render_cmd, batch_cmd, and compile_cmd.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.cli.exit_codes import ExitCode
from src.core.config import get_output_dir as config_get_output_dir
from src.core.schemas import CheckpointStatus, StageType
from src.core.storage import (
    StorageError,
    StorageManager,
    atomic_write_json,
    format_chapter_filename,
    get_chapter_dir,
    get_chapter_export_path,
    get_output_dir,
    get_workspace_dir,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    safe_cleanup_chapter_intermediates,
    stage_chapter_export,
    update_checkpoint,
)


class TestPhase4WorkspaceAndExportPaths:
    """Verifies standardized workspace, output directory, and export file naming."""

    def test_standardized_output_directory(self, tmp_path, monkeypatch):
        # Default behavior: output/<project_slug>
        monkeypatch.delenv("MANGA_RECAP_OUTPUT_DIR", raising=False)
        out1 = get_output_dir("test_series")
        assert out1 == Path("output") / "test_series"
        assert config_get_output_dir("test_series") == Path("output") / "test_series"
        assert StorageManager.get_output_dir("test_series") == Path("output") / "test_series"

        # Environment override
        custom_out = tmp_path / "custom_exports"
        monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(custom_out))
        out2 = get_output_dir("test_series")
        assert out2 == custom_out / "test_series"

    def test_standardized_workspace_directory(self):
        ws_root = get_workspace_dir("hero_manga")
        assert ws_root == Path("workspace") / "hero_manga"
        assert StorageManager.get_workspace_dir("hero_manga") == Path("workspace") / "hero_manga"

        ws_chap = get_workspace_dir("hero_manga", 3)
        assert ws_chap == Path("workspace") / "hero_manga" / "chapter_003"

        ws_str = get_workspace_dir("hero_manga", "12")
        assert ws_str == Path("workspace") / "hero_manga" / "chapter_012"

        ws_custom = get_workspace_dir("hero_manga", "prologue")
        assert ws_custom == Path("workspace") / "hero_manga" / "chapter_prologue"

    def test_chapter_export_filename_formatting(self):
        # Integer chapter numbers
        assert format_chapter_filename(1, ext="mp4", padded=True) == "chapter_001.mp4"
        assert format_chapter_filename(1, ext="mp4", padded=False) == "chapter_1.mp4"
        assert format_chapter_filename(42, ext="srt", padded=True) == "chapter_042.srt"
        assert format_chapter_filename(42, ext="srt", padded=False) == "chapter_42.srt"

        # String chapter numbers
        assert format_chapter_filename("5", ext=".mp4", padded=True) == "chapter_005.mp4"
        assert format_chapter_filename("100", ext="mp4", padded=True) == "chapter_100.mp4"

        # Non-numeric string chapter numbers
        assert format_chapter_filename("extra_1", ext="mp4") == "chapter_extra_1.mp4"

        # Full export path helper
        exp_path = get_chapter_export_path("sololeveling", 1, ext="mp4", padded=True)
        assert exp_path.name == "chapter_001.mp4"
        assert exp_path.parent.name == "sololeveling"

    def test_stage_chapter_export_single_standard_padded(self, tmp_path, monkeypatch):
        """Verifies staging creates only standardized padded chapter_007.mp4 without redundant duplicate copies."""
        out_root = tmp_path / "output"
        monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(out_root))

        src_video = tmp_path / "render_final.mp4"
        src_video.write_bytes(b"dummy_mp4_content" * 100)

        src_srt = tmp_path / "subtitles.srt"
        src_srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHello world\n", encoding="utf-8")

        staged = stage_chapter_export(
            project_slug="dual_series",
            chapter_num=7,
            video_path=src_video,
            subtitle_path=src_srt,
        )

        proj_out = out_root / "dual_series"
        assert (proj_out / "chapter_007.mp4").exists()
        assert not (proj_out / "chapter_7.mp4").exists()
        assert (proj_out / "chapter_007.srt").exists()
        assert not (proj_out / "chapter_7.srt").exists()

        assert (proj_out / "chapter_007.mp4").read_bytes() == src_video.read_bytes()
        assert staged["video_padded"] == proj_out / "chapter_007.mp4"


class TestIntermediateRetentionAndSafeCleanup:
    """Verifies retention rules and safe cleanup guarantees."""

    def test_retention_of_intermediate_data_during_processing_and_cache(self, tmp_path, monkeypatch):
        """Intermediate files (raw/, panels/, audio/clips/, manifest, script) must be preserved."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        slug = "retain_test"
        init_project_layout(slug, "Retention Project")
        chap_dir = init_chapter_layout(slug, 1)

        # Populate intermediate files
        raw_file = chap_dir / "raw" / "page_001.jpg"
        raw_file.write_bytes(b"raw_image_data")

        panel_file = chap_dir / "panels" / "panel_001.jpg"
        panel_file.write_bytes(b"panel_image_data")

        audio_clip = chap_dir / "audio" / "clips" / "seg_001.wav"
        audio_clip.write_bytes(b"audio_clip_data")

        manifest_file = chap_dir / "manifest.json"
        manifest_file.write_text('{"panels": []}', encoding="utf-8")

        script_file = chap_dir / "script.json"
        script_file.write_text('{"segments": []}', encoding="utf-8")

        # Mark chapter completed
        update_checkpoint(chap_dir, StageType.INGEST, CheckpointStatus.COMPLETED)
        update_checkpoint(chap_dir, StageType.AGENT, CheckpointStatus.COMPLETED)
        update_checkpoint(chap_dir, StageType.AUDIO, CheckpointStatus.COMPLETED)
        update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.COMPLETED)

        # Assert all intermediate assets are present and intact
        assert raw_file.exists()
        assert panel_file.exists()
        assert audio_clip.exists()
        assert manifest_file.exists()
        assert script_file.exists()
        assert (chap_dir / "chapter.json").exists()

    def test_safe_cleanup_behavior_when_output_verified(self, tmp_path):
        """Temporary files are cleaned while metadata and raw images are strictly safeguarded."""
        chap_dir = tmp_path / "chapter_1"
        chap_dir.mkdir(parents=True)
        (chap_dir / "raw").mkdir()
        (chap_dir / "panels").mkdir()
        (chap_dir / "video" / "temp_clips").mkdir(parents=True)
        (chap_dir / "video" / "frames").mkdir(parents=True)

        # Protected source and metadata files
        raw_img = chap_dir / "raw" / "raw_page_001.jpg"
        raw_img.write_bytes(b"raw_pixels")

        manifest = chap_dir / "manifest.json"
        manifest.write_text('{"panels": ["p1"]}', encoding="utf-8")

        script = chap_dir / "script.json"
        script.write_text('{"segments": ["s1"]}', encoding="utf-8")

        chap_json = chap_dir / "chapter.json"
        chap_json.write_text('{"status": "completed"}', encoding="utf-8")

        # Temporary files that should be cleaned
        temp_split_clip = chap_dir / "video" / "chapter_1_raw_temp_0.mp4"
        temp_split_clip.write_bytes(b"temp_clip_video")

        temp_subs = chap_dir / "video" / "chapter_1_subs_temp_0.srt"
        temp_subs.write_text("temp subtitle", encoding="utf-8")

        concat_list = chap_dir / "video" / "video_concat_list.txt"
        concat_list.write_text("file '...'", encoding="utf-8")

        temp_clip_sub = chap_dir / "video" / "temp_clips" / "clip_001.mp4"
        temp_clip_sub.write_bytes(b"clip_in_temp_dir")

        cached_frame = chap_dir / "video" / "frames" / "frame_001.png"
        cached_frame.write_bytes(b"frame_pixels")

        # Verified output video (>= 100,000 bytes)
        out_vid = tmp_path / "output_final.mp4"
        out_vid.write_bytes(b"V" * 150000)

        cleaned = safe_cleanup_chapter_intermediates(chap_dir, output_video_path=out_vid, min_size_bytes=100000)

        # Temporary files must be unlinked
        assert not temp_split_clip.exists()
        assert not temp_subs.exists()
        assert not concat_list.exists()
        assert not temp_clip_sub.exists()
        assert not cached_frame.exists()

        # Source data and metadata MUST NOT be deleted
        assert raw_img.exists()
        assert manifest.exists()
        assert script.exists()
        assert chap_json.exists()

        # Cleaned list contains unlinked filenames
        assert "chapter_1_raw_temp_0.mp4" in cleaned
        assert "clip_001.mp4" in cleaned

    def test_safe_cleanup_refusal_on_missing_output(self, tmp_path):
        """Cleanup MUST abort with StorageError if output video does not exist."""
        chap_dir = tmp_path / "chapter_1"
        chap_dir.mkdir(parents=True)
        temp_file = chap_dir / "chapter_1_raw_temp_0.mp4"
        temp_file.write_bytes(b"temp")

        missing_out = tmp_path / "does_not_exist.mp4"

        with pytest.raises(StorageError) as exc_info:
            safe_cleanup_chapter_intermediates(chap_dir, output_video_path=missing_out)

        assert "output video does not exist" in str(exc_info.value)
        # Verify no files were deleted
        assert temp_file.exists()

    def test_safe_cleanup_refusal_on_undersized_or_empty_output(self, tmp_path):
        """Cleanup MUST abort with StorageError if output video is below min_size_bytes."""
        chap_dir = tmp_path / "chapter_1"
        chap_dir.mkdir(parents=True)
        temp_file = chap_dir / "chapter_1_raw_temp_0.mp4"
        temp_file.write_bytes(b"temp")

        # 50 bytes is far below default 100,000 threshold
        undersized_out = tmp_path / "corrupted_or_partial.mp4"
        undersized_out.write_bytes(b"X" * 50)

        with pytest.raises(StorageError) as exc_info:
            safe_cleanup_chapter_intermediates(chap_dir, output_video_path=undersized_out, min_size_bytes=100000)

        assert "is below minimum threshold" in str(exc_info.value)
        # Verify no files were deleted
        assert temp_file.exists()


class TestCliIntegrationWorkspaceAndCleanup:
    """Verifies CLI integration for render, batch, and compile."""

    def test_render_cmd_stages_export_and_cleans_up_on_flag(self, tmp_path, monkeypatch):
        """manga-recap render --cleanup stages output to output/<slug>/ and safely cleans temp files."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        out_root = tmp_path / "output"
        monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(out_root))

        slug = "render_export_test"
        chap = "1"
        proj_dir = init_project_layout(slug, "Render Export Project")
        chap_dir = init_chapter_layout(slug, chap)

        # Create dummy script.json
        (chap_dir / "script.json").write_text('{"segments": []}', encoding="utf-8")

        # Mock VideoEngine render_chapter
        mock_vid = chap_dir / "video" / "chapter_1_final.mp4"
        mock_vid.parent.mkdir(parents=True, exist_ok=True)
        mock_vid.write_bytes(b"V" * 120000)

        mock_srt = chap_dir / "subtitles" / "chapter_1.srt"
        mock_srt.parent.mkdir(parents=True, exist_ok=True)
        mock_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nSub\n", encoding="utf-8")

        temp_clip = chap_dir / "chapter_1_raw_temp_0.mp4"
        temp_clip.write_bytes(b"temp_clip_data")

        from src.cli import render_cmd

        class FakeVideoEngine:
            def render_chapter(self, **kwargs):
                return {
                    "video_path": str(mock_vid),
                    "subtitle_path": str(mock_srt),
                    "resolution": "1920x1080",
                    "fps": 30.0,
                    "video_duration_seconds": 10.0,
                    "audio_duration_seconds": 10.0,
                    "duration_diff_seconds": 0.0,
                    "cleaned_files_count": 0,
                }

        monkeypatch.setattr(render_cmd, "VideoEngine", FakeVideoEngine)
        monkeypatch.setattr(render_cmd.shutil, "which", lambda cmd: "/usr/bin/ffmpeg")

        args = argparse.Namespace(
            project=slug,
            chapter=chap,
            quality="final",
            suffix="final",
            approved=True,
            cleanup=True,
            keep_temp=False,
        )

        res = render_cmd.handle_render(args)

        # Assert staging into output/<slug>/
        assert (out_root / slug / "chapter_001.mp4").exists()
        assert not (out_root / slug / "chapter_1.mp4").exists()
        assert (out_root / slug / "chapter_001.srt").exists()
        assert not (out_root / slug / "chapter_1.srt").exists()

        # Assert export paths in result
        assert res["export_video_path"] == str(out_root / slug / "chapter_001.mp4")
        assert res["export_subtitle_path"] == str(out_root / slug / "chapter_001.srt")

        # Assert temp clip was cleaned up by safe_cleanup_chapter_intermediates
        assert not temp_clip.exists()

    def test_batch_cmd_stages_export_and_respects_cleanup(self, tmp_path, monkeypatch):
        """manga-recap batch stages exports into output/<slug>/chapter_{xxx}.mp4 and respects --cleanup."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        out_root = tmp_path / "output"
        monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(out_root))

        slug = "batch_export_test"
        proj_dir = init_project_layout(slug, "Batch Export Project")
        chap1_dir = init_chapter_layout(slug, "1")
        chap2_dir = init_chapter_layout(slug, "2")

        from src.core.schemas import RecapScript, ScriptSegment
        for c, c_dir in [("1", chap1_dir), ("2", chap2_dir)]:
            script = RecapScript(
                project_slug=slug,
                chapter_number=c,
                title=f"Chap {c}",
                segments=[ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text=f"Text for chapter {c}")],
            )
            atomic_write_json(c_dir / "script.json", script)

        from src.cli import batch_cmd, render_cmd, tts_cmd

        monkeypatch.setattr(tts_cmd, "handle_tts", lambda a: {"synthesized": 1, "reused": 0})

        def mock_render(a):
            c_dir = get_chapter_dir(slug, a.chapter)
            vid = c_dir / "video" / f"chapter_{a.chapter}_final.mp4"
            vid.parent.mkdir(parents=True, exist_ok=True)
            vid.write_bytes(b"M" * 120000)
            srt = c_dir / "subtitles" / f"chapter_{a.chapter}.srt"
            srt.parent.mkdir(parents=True, exist_ok=True)
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nSub\n", encoding="utf-8")
            return {"video_path": str(vid), "subtitle_path": str(srt), "quality": "final"}

        monkeypatch.setattr(render_cmd, "handle_render", mock_render)

        batch_args = argparse.Namespace(
            project=slug,
            chapters="1,2",
            approved=True,
            cleanup=True,
            skip_preview=True,
            compile=False,
            force=False,
            allow_placeholders=True,
        )

        res = batch_cmd.handle_batch(batch_args)
        assert res["success"] is True

        # Verify staged exports in output/<slug>/ with standardized padded format
        assert (out_root / slug / "chapter_001.mp4").exists()
        assert not (out_root / slug / "chapter_1.mp4").exists()
        assert (out_root / slug / "chapter_002.mp4").exists()
        assert not (out_root / slug / "chapter_2.mp4").exists()

    def test_compile_cmd_stages_final_recap_to_output_dir(self, tmp_path, monkeypatch):
        """manga-recap compile automatically copies/stages compiled recap to output/<slug>/."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        out_root = tmp_path / "output"
        monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(out_root))

        slug = "compile_export_test"
        proj_dir = init_project_layout(slug, "Compile Export Project")
        chap1_dir = init_chapter_layout(slug, "1")
        chap2_dir = init_chapter_layout(slug, "2")

        v1 = chap1_dir / "video" / "chapter_1_final.mp4"
        v1.parent.mkdir(parents=True, exist_ok=True)
        v1.write_bytes(b"vid1")

        v2 = chap2_dir / "video" / "chapter_2_final.mp4"
        v2.parent.mkdir(parents=True, exist_ok=True)
        v2.write_bytes(b"vid2")

        from src.cli import compile_cmd

        monkeypatch.setattr(compile_cmd.shutil, "which", lambda cmd: "/usr/bin/ffmpeg")

        with patch("src.cli.compile_cmd.run_ffmpeg") as mock_ffmpeg:
            mock_ffmpeg.return_value = 0

            # Write compiled video to destination when called
            def fake_run_ffmpeg(cmd, **kwargs):
                out_target = Path(cmd[-1])
                out_target.parent.mkdir(parents=True, exist_ok=True)
                out_target.write_bytes(b"C" * 200000)
                return 0

            mock_ffmpeg.side_effect = fake_run_ffmpeg

            compile_args = argparse.Namespace(
                project=slug,
                chapters="1,2",
                output=None,
                approved=True,
                extract_audio=False,
            )

            res = compile_cmd.handle_compile(compile_args)

            # Assert staged recap in output/<slug>/
            staged_recap = out_root / slug / f"{slug}_full_recap.mp4"
            assert staged_recap.exists()
            assert res["export_video_path"] == str(staged_recap)
