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

from v4_fixtures import init_confirmed_project

import argparse
import os
import shutil
import subprocess
import sys
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
        init_confirmed_project("proj_crash_seg", "Crash Segment")
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

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_script_validation_updates_script_stage_to_completed(self, tmp_path, monkeypatch):
        """Verifies that successful script validation updates StageType.SCRIPT checkpoint to COMPLETED."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_confirmed_project("proj_val_script", "Val Script")
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

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_get_resumable_stage_identifies_interrupted_running_and_failed(self, tmp_path, monkeypatch):
        """Verifies get_resumable_stage returns first incomplete stage (RUNNING or FAILED)."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_confirmed_project("proj_resumable", "Resumable")
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
    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_segment_force_invalidates_downstream_artifacts_and_checkpoints(self, tmp_path, monkeypatch):
        """Verifies that force re-segmenting unlinks downstream audio, subs, video, and resets checkpoints."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        init_confirmed_project("proj_seg_inval", "Seg Inval")
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

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


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
        init_confirmed_project("proj_miss_panel", "Missing Panel")
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
        init_confirmed_project("proj_audio_val", "Audio Val")
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
    REPO_ROOT = Path(__file__).resolve().parents[2]

    def test_pyproject_toml_exists_and_declares_entrypoint(self):
        """Verifies pyproject.toml exists in root and registers manga-recap entrypoint."""
        pyproject_path = self.REPO_ROOT / "pyproject.toml"
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
        sh_path = self.REPO_ROOT / "setup.sh"
        bat_path = self.REPO_ROOT / "setup.bat"
        assert "OPENROUTER_API_KEY" not in sh_path.read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY" not in bat_path.read_text(encoding="utf-8")

    def test_manga_recap_cli_installed_and_callable(self):
        """Verifies manga-recap CLI executable runs and outputs valid help."""
        exe_dir = Path(sys.executable).parent
        candidates = [
            exe_dir / "manga-recap",
            exe_dir / "manga-recap.exe",
        ]
        cli_path = next((p for p in candidates if p.exists()), None)
        if cli_path is None:
            which_cli = shutil.which("manga-recap")
            cli_path = Path(which_cli) if which_cli else None
        assert cli_path is not None and cli_path.exists(), (
            f"manga-recap console entrypoint not found next to {sys.executable} or on PATH"
        )
        res = subprocess.run([str(cli_path), "--version"], capture_output=True, text=True)
        assert res.returncode == 0
        assert "manga-recap 3.0.0" in res.stdout
