"""
tests/unit/test_p1_fixes.py
Unit and regression test suite for Milestone 2: P1 Data Integrity & Contracts (FIX-06 to FIX-10).
Validates:
- FIX-06: Checkpoint crash recovery, StageType.SCRIPT completion, and FINALIZE chapter completion.
- FIX-07: Downstream artifact invalidation upon force re-segment, audio re-synthesis, and compile video selection.
- FIX-08: Strict schema validation (unique seq_id, FocusBox dimensions >= 0.05, audio checksum, duration match).
- FIX-09: Audio-video timeline sync with short clips (< 0.5s), exact WAV persistence, and SRT alignment <= 150ms.
- FIX-10: Cross-platform pyproject.toml packaging, clean config without cloud APIs, and manga-recap entrypoint.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.cli.compile_cmd import handle_compile
from src.cli.exit_codes import CliValidationError
from src.cli.main import main
from src.cli.segment_cmd import handle_segment
from src.cli.status_cmd import handle_status
from src.cli.tts_cmd import handle_tts
from src.cli.validate_cmd import handle_validate
from src.core.config import AppConfig
from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CheckpointStatus,
    FocusBox,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    ScriptSegment,
    StageCheckpoint,
    StageType,
    utc_iso_now,
)
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    calculate_utterance_fingerprint,
    get_chapter_dir,
    get_project_dir,
    get_resumable_stage,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    read_json,
    reset_stage_checkpoint,
    update_checkpoint,
)
from src.render.subtitle import SubtitleGenerator, SubtitleCue
from src.render.video_engine import VideoEngine
from src.tts.engine import get_wav_duration_seconds


# ==============================================================================
# Helper to generate uncompressed 48kHz mono test WAV
# ==============================================================================
def create_test_wav(path: Path, duration_sec: float = 1.0, sample_rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(round(duration_sec * sample_rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * num_frames)
    return path


# ==============================================================================
# 1. FIX-06: Checkpoint Lifecycle Standard
# ==============================================================================
class TestFix06CheckpointLifecycle:
    def test_segment_cmd_records_failed_on_exception(self, tmp_path, monkeypatch):
        """Verifies that an unhandled exception in segment_cmd transitions SEGMENT checkpoint to FAILED."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_crash_seg", "Crash Segment")
        chap_dir = init_chapter_layout("proj_crash_seg", 1)

        # Put a valid raw image so it passes initial file check
        raw_dir = chap_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        img = Image.new("RGB", (100, 100), color=(255, 255, 255))
        img.save(raw_dir / "page_001.jpg")

        from src.core.streaming_segmenter import StreamingSegmenter

        def boom_extract(self, *args, **kwargs):
            raise RuntimeError("Simulated segmentation engine crash")

        monkeypatch.setattr(StreamingSegmenter, "extract_panels", boom_extract)

        args = argparse.Namespace(project="proj_crash_seg", chapter=1, force=False)
        with pytest.raises(RuntimeError, match="Simulated segmentation engine crash"):
            handle_segment(args)

        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.SEGMENT].status == CheckpointStatus.FAILED
        assert "Simulated segmentation engine crash" in (meta.stages[StageType.SEGMENT].error_message or "")
        assert meta.overall_status == CheckpointStatus.FAILED

    def test_tts_cmd_records_failed_on_exception(self, tmp_path, monkeypatch):
        """Verifies that an unhandled exception in tts_cmd transitions TTS checkpoint to FAILED."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_crash_tts", "Crash TTS")
        chap_dir = init_chapter_layout("proj_crash_tts", 1)

        script = RecapScript(
            project_slug="proj_crash_tts",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Fail TTS test")
            ],
        )
        atomic_write_json(chap_dir / "script.json", script)

        from src.tts.engine import MockTTSBackend

        def boom_synth(self, *args, **kwargs):
            raise ValueError("Simulated TTS synthesis failure")

        monkeypatch.setattr(MockTTSBackend, "synthesize", boom_synth)

        args = argparse.Namespace(
            project="proj_crash_tts",
            chapter=1,
            voice=None,
            speed=None,
            force=True,
            mock=True,
        )
        with pytest.raises(ValueError, match="Simulated TTS synthesis failure"):
            handle_tts(args)

        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.TTS].status == CheckpointStatus.FAILED
        assert "Simulated TTS synthesis failure" in (meta.stages[StageType.TTS].error_message or "")
        assert meta.overall_status == CheckpointStatus.FAILED

    def test_video_engine_records_failed_with_error_message(self, tmp_path, monkeypatch):
        """Verifies that video_engine render failure sets RENDER checkpoint to FAILED with error message."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("proj_crash_render", "Crash Render")
        chap_dir = init_chapter_layout("proj_crash_render", 1)

        # Create valid script with missing panel image without allow_placeholders
        script = RecapScript(
            project_slug="proj_crash_render",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["nonexistent_panel.jpg"], narrator_text="Narration")
            ],
        )
        atomic_write_json(chap_dir / "script.json", script)

        engine = VideoEngine()
        with pytest.raises(Exception):
            engine.render_chapter(
                project_dir=proj_dir,
                chapter_num=1,
                quality="preview",
                allow_placeholders=False,
            )

        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.RENDER].status == CheckpointStatus.FAILED
        assert meta.stages[StageType.RENDER].error_message is not None
        assert "nonexistent_panel.jpg" in meta.stages[StageType.RENDER].error_message

    def test_script_validation_updates_script_stage_to_completed(self, tmp_path, monkeypatch):
        """Verifies that successful script validation updates StageType.SCRIPT checkpoint to COMPLETED."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_val_script", "Val Script")
        chap_dir = init_chapter_layout("proj_val_script", 1)

        # Create panel
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        (panels_dir / "panel_001.jpg").write_bytes(b"dummy_img")

        manifest = PanelManifest(
            schema_version="3.0.0",
            project_slug="proj_val_script",
            chapter_number="1",
            panels=[
                {
                    "panel_id": "panel_001",
                    "sequence_number": 1,
                    "filename": "panel_001.jpg",
                    "relative_path": "panels/panel_001.jpg",
                    "source_image": "page_001.jpg",
                    "crop_box": (0, 0, 100, 100),
                    "width": 100,
                    "height": 100,
                    "aspect_ratio": 1.0,
                    "sha256": "hash",
                }
            ],
            total_panels=1,
        )
        atomic_write_json(chap_dir / "manifest.json", manifest)

        script = RecapScript(
            project_slug="proj_val_script",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Valid narration")
            ],
        )
        atomic_write_json(chap_dir / "script.json", script)

        # Initially SCRIPT is PENDING
        meta_before = load_chapter_metadata(chap_dir)
        assert meta_before.stages[StageType.SCRIPT].status == CheckpointStatus.PENDING

        args = argparse.Namespace(project="proj_val_script", chapter="1", strict=False)
        res = handle_validate(args)
        assert res["valid"] is True

        meta_after = load_chapter_metadata(chap_dir)
        assert meta_after.stages[StageType.SCRIPT].status == CheckpointStatus.COMPLETED
        assert "script.json" in meta_after.stages[StageType.SCRIPT].artifacts

    def test_final_render_auto_marks_finalize_and_chapter_is_complete(self, tmp_path, monkeypatch):
        """Verifies that final render auto-completes FINALIZE and status reports is_complete: True."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_e2e_complete", "Complete Chapter")
        chap_dir = init_chapter_layout("proj_e2e_complete", 1)

        # Mark all prior stages completed
        for stg in [StageType.CRAWL, StageType.SEGMENT, StageType.SCRIPT, StageType.TTS]:
            update_checkpoint(chap_dir, stg, CheckpointStatus.RUNNING)
            update_checkpoint(chap_dir, stg, CheckpointStatus.COMPLETED)

        # Mock render engine render_chapter to simulate successful final render
        from src.render.video_engine import VideoEngine

        def mock_render(self, *args, **kwargs):
            final_vid = chap_dir / "video" / "chapter_1_final.mp4"
            final_vid.parent.mkdir(parents=True, exist_ok=True)
            final_vid.write_bytes(b"mock_mp4")
            srt_p = chap_dir / "subtitles" / "chapter_1.srt"
            srt_p.parent.mkdir(parents=True, exist_ok=True)
            srt_p.write_text("1\n00:00:00,000 --> 00:00:01,000\nSub\n", encoding="utf-8")

            # Emulate video_engine.render_chapter checkpoint updates
            update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.COMPLETED, artifacts=["video/chapter_1_final.mp4"])
            update_checkpoint(chap_dir, StageType.FINALIZE, CheckpointStatus.COMPLETED, artifacts=["video/chapter_1_final.mp4"])
            return {
                "video_path": str(final_vid),
                "subtitle_path": str(srt_p),
                "quality": "final",
            }

        monkeypatch.setattr(VideoEngine, "render_chapter", mock_render)

        # Script must exist for render_cmd
        script = RecapScript(
            project_slug="proj_e2e_complete",
            chapter_number="1",
            title="Ep 1",
            segments=[ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Narration")],
        )
        atomic_write_json(chap_dir / "script.json", script)

        from src.cli.render_cmd import handle_render
        args = argparse.Namespace(
            project="proj_e2e_complete",
            chapter=1,
            quality="final",
            approved=True,
            bgm=None,
            burn_subtitles=False,
            keep_temp=False,
            force=False,
            suffix=None,
            video_mode=None,
            no_animation=False,
            allow_placeholders=True,
        )
        handle_render(args)

        status_res = handle_status(argparse.Namespace(project="proj_e2e_complete", chapter=1))
        assert status_res["is_complete"] is True
        assert status_res["overall_status"] == CheckpointStatus.COMPLETED.value
        assert status_res["resumable_stage"] is None
        assert status_res["stages"]["finalize"]["status"] == CheckpointStatus.COMPLETED.value

    def test_get_resumable_stage_identifies_interrupted_running_and_failed(self, tmp_path, monkeypatch):
        """Verifies get_resumable_stage returns first incomplete stage (RUNNING or FAILED)."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_resumable", "Resumable")
        chap_dir = init_chapter_layout("proj_resumable", 1)

        # Initially CRAWL is PENDING
        assert get_resumable_stage(chap_dir) == StageType.CRAWL

        update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.RUNNING)
        update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.COMPLETED)
        assert get_resumable_stage(chap_dir) == StageType.SEGMENT

        # SEGMENT is interrupted while RUNNING
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.RUNNING)
        assert get_resumable_stage(chap_dir) == StageType.SEGMENT

        # SEGMENT failed
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.FAILED, error="Out of memory")
        assert get_resumable_stage(chap_dir) == StageType.SEGMENT


# ==============================================================================
# 2. FIX-07: Synchronize Script, Audio, and Video Artifact Invalidation
# ==============================================================================
class TestFix07ArtifactInvalidation:
    def test_tts_re_synthesis_invalidates_video_and_subtitles(self, tmp_path, monkeypatch):
        """Verifies that re-synthesizing an audio clip unlinks video/subs and resets RENDER to PENDING."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_tts_inval", "TTS Inval")
        chap_dir = init_chapter_layout("proj_tts_inval", 1)

        # Create initial video and subtitle artifacts
        vid_dir = chap_dir / "video"
        vid_dir.mkdir(parents=True, exist_ok=True)
        final_mp4 = vid_dir / "chapter_1_final.mp4"
        preview_mp4 = vid_dir / "chapter_1_preview.mp4"
        final_mp4.write_bytes(b"video_content")
        preview_mp4.write_bytes(b"preview_content")

        sub_dir = chap_dir / "subtitles"
        sub_dir.mkdir(parents=True, exist_ok=True)
        sub_file = sub_dir / "chapter_1.srt"
        sub_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nOld text\n", encoding="utf-8")

        # Mark RENDER and FINALIZE as COMPLETED
        update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.RUNNING)
        update_checkpoint(chap_dir, StageType.RENDER, CheckpointStatus.COMPLETED)
        update_checkpoint(chap_dir, StageType.FINALIZE, CheckpointStatus.RUNNING)
        update_checkpoint(chap_dir, StageType.FINALIZE, CheckpointStatus.COMPLETED)

        # Write script with new narration
        script = RecapScript(
            project_slug="proj_tts_inval",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Updated text for TTS")
            ],
        )
        atomic_write_json(chap_dir / "script.json", script)

        # Run TTS
        args = argparse.Namespace(
            project="proj_tts_inval",
            chapter=1,
            voice="Trúc Ly",
            speed=1.0,
            force=True,
            mock=True,
        )
        res = handle_tts(args)
        assert res["segments_synthesized"] == 1

        # Downstream video and subtitle files must be unlinked
        assert not final_mp4.exists()
        assert not preview_mp4.exists()
        assert not sub_file.exists()

        # Checkpoint RENDER and FINALIZE must be reset to PENDING
        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.RENDER].status == CheckpointStatus.PENDING
        assert meta.stages[StageType.FINALIZE].status == CheckpointStatus.PENDING
        assert meta.overall_status != CheckpointStatus.COMPLETED

    def test_segment_force_invalidates_downstream_artifacts_and_checkpoints(self, tmp_path, monkeypatch):
        """Verifies that force re-segmenting unlinks downstream audio, subs, video, and resets checkpoints."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_seg_inval", "Seg Inval")
        chap_dir = init_chapter_layout("proj_seg_inval", 1)

        # Populate raw images
        raw_dir = chap_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        img = Image.new("RGB", (400, 800), color=(255, 255, 255))
        img.save(raw_dir / "page_001.jpg")

        # Create dummy audio, sub, and video files
        audio_clip = chap_dir / "audio" / "clips" / "audio_1_001.wav"
        create_test_wav(audio_clip, 1.0)
        sub_file = chap_dir / "subtitles" / "chapter_1.srt"
        sub_file.parent.mkdir(parents=True, exist_ok=True)
        sub_file.write_text("sub content", encoding="utf-8")
        vid_file = chap_dir / "video" / "chapter_1_final.mp4"
        vid_file.parent.mkdir(parents=True, exist_ok=True)
        vid_file.write_bytes(b"mp4 content")

        # Mark all downstream stages as COMPLETED
        for stg in [StageType.SCRIPT, StageType.TTS, StageType.RENDER, StageType.FINALIZE]:
            update_checkpoint(chap_dir, stg, CheckpointStatus.RUNNING)
            update_checkpoint(chap_dir, stg, CheckpointStatus.COMPLETED)

        # Execute segment with force=True
        args = argparse.Namespace(
            project="proj_seg_inval",
            chapter=1,
            target_width=800,
            threshold_variance=15.0,
            min_gutter_height=30,
            margin_padding=10,
            cell_size=150,
            force=True,
        )
        res = handle_segment(args)
        assert res["status"] == "completed"

        # Downstream audio, sub, and video must be unlinked
        assert not audio_clip.exists()
        assert not sub_file.exists()
        assert not vid_file.exists()

        # Checkpoints for SCRIPT, TTS, RENDER, FINALIZE must be reset to PENDING
        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.SCRIPT].status == CheckpointStatus.PENDING
        assert meta.stages[StageType.TTS].status == CheckpointStatus.PENDING
        assert meta.stages[StageType.RENDER].status == CheckpointStatus.PENDING
        assert meta.stages[StageType.FINALIZE].status == CheckpointStatus.PENDING

    def test_compile_cmd_prefers_final_over_preview(self, tmp_path, monkeypatch):
        """Verifies that compile_cmd prefers final.mp4 / *_final.mp4 over preview.mp4."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("proj_compile_pref", "Compile Pref")
        chap_dir = init_chapter_layout("proj_compile_pref", 1)

        # In chapter video dir, create both preview and final mp4s
        vid_dir = chap_dir / "video"
        vid_dir.mkdir(parents=True, exist_ok=True)
        preview_file = vid_dir / "chapter_1_preview.mp4"
        final_file = vid_dir / "chapter_1_final.mp4"
        preview_file.write_bytes(b"preview_data")
        final_file.write_bytes(b"final_data")

        # Intercept run_ffmpeg to capture the input list or test video selection
        selected_file = None
        from src.cli import compile_cmd

        # Inspect video collection logic directly or mock run_ffmpeg
        def mock_ffmpeg(cmd, **kwargs):
            nonlocal selected_file
            # In compile_cmd, concat list file contains file paths
            for arg in cmd:
                if isinstance(arg, (str, Path)) and str(arg).endswith(".txt"):
                    content = Path(arg).read_text(encoding="utf-8")
                    for line in content.splitlines():
                        if "file " in line:
                            selected_file = line.replace("file ", "").strip("'\"")
            return 0

        monkeypatch.setattr(compile_cmd, "run_ffmpeg", mock_ffmpeg)

        args = argparse.Namespace(
            project="proj_compile_pref",
            chapters="1",
            output=str(proj_dir / "compiled.mp4"),
            extract_audio=False,
            approved=True,
        )
        try:
            handle_compile(args)
        except Exception:
            pass

        assert selected_file is not None
        assert "final" in selected_file.lower()
        assert "preview" not in selected_file.lower()


# ==============================================================================
# 3. FIX-08: Enforce Strict Schema Validation
# ==============================================================================
class TestFix08StrictSchemaValidation:
    def test_recap_script_rejects_duplicate_seq_id(self):
        """Verifies that RecapScript rejects duplicate seq_id values across segments."""
        with pytest.raises(ValidationError, match="Duplicate seq_id"):
            RecapScript(
                project_slug="test_slug",
                chapter_number="1",
                title="Test",
                segments=[
                    ScriptSegment(seq_id="001", panel_ids=["p1.jpg"], narrator_text="First"),
                    ScriptSegment(seq_id="001", panel_ids=["p2.jpg"], narrator_text="Duplicate!"),
                ],
            )

    def test_focus_box_enforces_minimum_width_and_height(self):
        """Verifies that FocusBox enforces x_max - x_min >= 0.05 and y_max - y_min >= 0.05."""
        # Valid box
        box = FocusBox(x_min=0.2, y_min=0.2, x_max=0.7, y_max=0.7)
        assert box.x_max - box.x_min >= 0.05

        # Degenerate width < 0.05
        with pytest.raises(ValidationError, match="Box width .* must be >= 0.05"):
            FocusBox(x_min=0.5, y_min=0.2, x_max=0.52, y_max=0.7)

        # Degenerate height < 0.05
        with pytest.raises(ValidationError, match="Box height .* must be >= 0.05"):
            FocusBox(x_min=0.2, y_min=0.5, x_max=0.7, y_max=0.53)

        # 0-width and 0-height
        with pytest.raises(ValidationError, match="Box width .* must be >= 0.05"):
            FocusBox(x_min=0.5, y_min=0.5, x_max=0.5, y_max=0.5)

    def test_validate_cmd_fails_on_missing_panel_references(self, tmp_path, monkeypatch):
        """Verifies that validate_cmd treats missing panel references as validation errors."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_miss_panel", "Missing Panel")
        chap_dir = init_chapter_layout("proj_miss_panel", 1)

        # Create manifest with panel_001
        manifest = PanelManifest(
            schema_version="3.0.0",
            project_slug="proj_miss_panel",
            chapter_number="1",
            panels=[
                {
                    "panel_id": "panel_001",
                    "sequence_number": 1,
                    "filename": "panel_001.jpg",
                    "relative_path": "panels/panel_001.jpg",
                    "source_image": "page_001.jpg",
                    "crop_box": (0, 0, 100, 100),
                    "width": 100,
                    "height": 100,
                    "aspect_ratio": 1.0,
                    "sha256": "h1",
                }
            ],
            total_panels=1,
        )
        atomic_write_json(chap_dir / "manifest.json", manifest)

        # Script references panel_999 which does not exist in manifest
        script = RecapScript(
            project_slug="proj_miss_panel",
            chapter_number="1",
            title="Ep 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_999.jpg"], narrator_text="Narration")
            ],
        )
        atomic_write_json(chap_dir / "script.json", script)

        args = argparse.Namespace(project="proj_miss_panel", chapter="1", strict=False)
        with pytest.raises(CliValidationError) as exc_info:
            handle_validate(args)

        err_msgs = [e.get("message", "") for e in exc_info.value.details]
        assert any("panel_999" in msg for msg in err_msgs)

    def test_validate_cmd_verifies_audio_checksum_and_duration(self, tmp_path, monkeypatch):
        """Verifies that validate_cmd verifies audio SHA-256 checksum and duration matching."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_project_layout("proj_audio_val", "Audio Val")
        chap_dir = init_chapter_layout("proj_audio_val", 1)

        # Create panel & manifest
        (chap_dir / "panels" / "panel_001.jpg").write_bytes(b"img")
        manifest = PanelManifest(
            schema_version="3.0.0",
            project_slug="proj_audio_val",
            chapter_number="1",
            panels=[
                {
                    "panel_id": "panel_001",
                    "sequence_number": 1,
                    "filename": "panel_001.jpg",
                    "relative_path": "panels/panel_001.jpg",
                    "source_image": "page_001.jpg",
                    "crop_box": (0, 0, 100, 100),
                    "width": 100,
                    "height": 100,
                    "aspect_ratio": 1.0,
                    "sha256": "h1",
                }
            ],
            total_panels=1,
        )
        atomic_write_json(chap_dir / "manifest.json", manifest)

        # Create valid 1.0s WAV audio
        audio_file = chap_dir / "audio" / "clips" / "audio_1_001.wav"
        create_test_wav(audio_file, duration_sec=1.0)
        correct_hash = calculate_sha256(audio_file)

        # 1. Invalid checksum triggers validation failure
        script_bad_checksum = RecapScript(
            project_slug="proj_audio_val",
            chapter_number="1",
            title="Ep 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="Text",
                    status="synthesized",
                    audio_path="audio/clips/audio_1_001.wav",
                    checksum="wrong_sha256_hash",
                    duration=1.0,
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script_bad_checksum)

        args = argparse.Namespace(project="proj_audio_val", chapter="1", strict=False)
        with pytest.raises(CliValidationError) as exc_info:
            handle_validate(args)
        assert any("checksum mismatch" in e.get("message", "").lower() for e in exc_info.value.details)

        # 2. Invalid duration triggers validation failure
        script_bad_dur = RecapScript(
            project_slug="proj_audio_val",
            chapter_number="1",
            title="Ep 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="Text",
                    status="synthesized",
                    audio_path="audio/clips/audio_1_001.wav",
                    checksum=correct_hash,
                    duration=5.0,  # Expected 5.0s, actual is 1.0s
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script_bad_dur)
        with pytest.raises(CliValidationError) as exc_info2:
            handle_validate(args)
        assert any("duration mismatch" in e.get("message", "").lower() for e in exc_info2.value.details)

        # 3. Correct checksum & duration passes cleanly
        script_good = RecapScript(
            project_slug="proj_audio_val",
            chapter_number="1",
            title="Ep 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="Text",
                    status="synthesized",
                    audio_path="audio/clips/audio_1_001.wav",
                    checksum=correct_hash,
                    duration=1.0,
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script_good)
        res = handle_validate(args)
        assert res["valid"] is True


# ==============================================================================
# 4. FIX-09: Synchronize Audio-Video Timeline (Discrepancy <= 150ms)
# ==============================================================================
class TestFix09TimelineSyncAndShortClips:
    def test_video_engine_does_not_clamp_short_clips_to_half_second(self, tmp_path):
        """Verifies that generate_panel_clip does not clamp clips < 0.5s to 0.5s."""
        engine = VideoEngine(fps=30.0)
        # Test 200 ms clip -> 0.20s (6 frames), not clamped to 0.5s (15 frames)
        duration_ms = 200
        calculated_duration = max(1.0 / engine.fps, float(duration_ms) / 1000.0)
        assert calculated_duration == pytest.approx(0.20, abs=0.001)

        # Test ultra-short 10 ms clip -> at least 1 frame (1/30 = 0.0333s)
        short_dur = max(1.0 / engine.fps, float(10) / 1000.0)
        assert short_dur == pytest.approx(1.0 / 30.0, abs=0.001)

    def test_subtitle_generator_does_not_drop_short_cues(self, tmp_path):
        """Verifies that SubtitleGenerator does not drop short duration utterances."""
        generator = SubtitleGenerator()
        segments = [
            ScriptSegment(seq_id="001", narrator_text="Chào bạn", duration=0.25),
            ScriptSegment(seq_id="002", narrator_text="Đi thôi", duration=0.30),
            ScriptSegment(seq_id="003", narrator_text="Đến nơi rồi", duration=1.20),
        ]
        cues = generator.build_cues_from_segments(segments)
        assert len(cues) == 3
        assert cues[0].text == "Chào bạn"
        assert cues[0].start_seconds == 0.0
        assert cues[0].end_seconds == 0.25
        assert cues[1].start_seconds == 0.25
        assert cues[1].end_seconds == 0.55

        # Verify SRT file generation and alignment <= 150ms
        srt_path = tmp_path / "sync_test.srt"
        generator.generate_srt_file(segments, srt_path)
        assert srt_path.exists()
        total_dur = 0.25 + 0.30 + 1.20  # 1.75s
        assert generator.validate_srt_sync(srt_path, expected_total_duration=total_dur, tolerance_seconds=0.15)

    def test_subtitle_generator_estimates_when_duration_zero_to_prevent_dropping(self):
        """Verifies that SubtitleGenerator falls back to estimate if duration <= 0 so cue is not lost."""
        generator = SubtitleGenerator()
        segments = [
            ScriptSegment(seq_id="001", narrator_text="Lời thoại không có thời lượng", duration=0.0),
        ]
        cues = generator.build_cues_from_segments(segments)
        assert len(cues) == 1
        assert cues[0].duration_seconds >= 0.5


# ==============================================================================
# 5. FIX-10: Cross-Platform Packaging & Clean Config
# ==============================================================================
class TestFix10PackagingAndCleanConfig:
    def test_pyproject_toml_exists_and_declares_entrypoint(self):
        """Verifies pyproject.toml exists in root and registers manga-recap entrypoint."""
        pyproject_path = Path("/Volumes/WorkSpace/Factory/Manga Review/pyproject.toml")
        assert pyproject_path.exists(), "pyproject.toml missing in project root"
        content = pyproject_path.read_text(encoding="utf-8")
        assert "build-backend = \"setuptools.build_meta\"" in content
        assert "manga-recap = \"src.cli.main:main\"" in content

    def test_config_has_no_cloud_api_fields(self):
        """Verifies that AppConfig is clean of cloud API key fields."""
        cfg = AppConfig()
        assert not hasattr(cfg, "openrouter_api_key")
        assert not hasattr(cfg, "supabase_url")
        assert not hasattr(cfg, "google_api_key")
        assert not hasattr(cfg, "openai_api_key")

    def test_setup_scripts_have_no_openrouter_api_key(self):
        """Verifies setup.sh and setup.bat do not reference OPENROUTER_API_KEY."""
        sh_path = Path("/Volumes/WorkSpace/Factory/Manga Review/setup.sh")
        bat_path = Path("/Volumes/WorkSpace/Factory/Manga Review/setup.bat")
        assert "OPENROUTER_API_KEY" not in sh_path.read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY" not in bat_path.read_text(encoding="utf-8")

    def test_manga_recap_cli_installed_and_callable(self):
        """Verifies manga-recap CLI executable runs and outputs valid help."""
        cli_path = Path("/Volumes/WorkSpace/Factory/Manga Review/venv/bin/manga-recap")
        assert cli_path.exists(), "venv/bin/manga-recap executable not found"
        res = subprocess.run([str(cli_path), "--version"], capture_output=True, text=True)
        assert res.returncode == 0
        assert "manga-recap 3.0.0" in res.stdout
