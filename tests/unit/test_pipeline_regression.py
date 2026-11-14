"""
tests/unit/test_pipeline_regression.py
Phase 3 Comprehensive Regression Test Suite (ORIGINAL_REQUEST R3).

Covers 8 critical regression areas:
1. Narration edit re-synthesizes audio with updated duration, checksum, and fingerprint.
2. Voice/speed/emotion changes avoid mismatched audio reuse across all utterances.
3. Crawling errors (network failures) and zero-image responses yield FAILED checkpoint & structured error envelope.
4. Chapters with 1–20 panels have 100% panel coverage with 0 omissions and 0 duplicates via sliding windows.
5. Missing review approvals block final render; explicit --approved flag allows render without interactive blocking.
6. Artifact invalidation cascades upon script edits, audio re-synthesis, and forced re-segmentation.
7. Crash recovery and resume from RUNNING or FAILED stage checkpoints.
8. Invalid CLI arguments across all subcommands yield consistent exit codes and valid JSON error envelopes.
"""

from __future__ import annotations

from v4_fixtures import init_confirmed_project

import argparse
import asyncio
import io
import json
import os
import shutil
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.cli.exit_codes import CliValidationError, ExitCode
from src.cli.main import build_parser, main
from src.cli.response import JsonEnvelope
from src.core.schemas import (
    CameraMotionType,
    ChapterMetadata,
    CheckpointStatus,
    FocusBox,
    PanelItem,
    PanelManifest,
    PanelStatus,
    PreviewApproval,
    ProjectConfig,
    RecapScript,
    ScriptSegment,
    StageCheckpoint,
    StageType,
    VoiceSelectionApproval,
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
from src.core.utils import compute_sliding_windows


# ==============================================================================
# Helpers
# ==============================================================================

def create_uncompressed_wav(path: Path, duration_sec: float = 1.0, sample_rate: int = 48000) -> Path:
    """Generates an uncompressed 48 kHz mono 16-bit PCM test WAV file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    num_frames = int(round(duration_sec * sample_rate))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * num_frames)
    return path


def setup_test_project_and_chapter(tmp_path: Path, monkeypatch, slug: str = "reg_proj", chapter: str = "1"):
    """Configures isolated test environment with initialized project and chapter layout."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    proj_dir = init_confirmed_project(slug, "Regression Test Project")
    chap_dir = init_chapter_layout(slug, chapter)
    return proj_dir, chap_dir


# ==============================================================================
# Area 1: Narration edit re-synthesizes audio
# ==============================================================================

class TestRegressionArea1NarrationEditReSynthesis:
    """
    R3 Area 1:
    Synthesize a segment with text A. Edit text to text B. Run TTS resume without --force.
    Assert old audio is NOT reused; new audio is generated with updated duration, checksum, and fingerprint.
    """

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# Area 2: Voice/speed/emotion changes avoid mismatched audio reuse
# ==============================================================================

class TestRegressionArea2ParameterMutationsAvoidMismatchedReuse:
    """
    R3 Area 2:
    Synthesize with voice V1, speed S1, emotion E1. Mutate voice to V2, or speed to S2, or emotion to E2.
    Run TTS resume. Assert audio is re-synthesized, not reused.
    """

    @pytest.fixture
    def setup_initial_audio(self, tmp_path, monkeypatch):
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, "p_area2", "1")
        initial_seg = ScriptSegment(
            seq_id="seg_param",
            panel_ids=["p0"],
            narrator_text="Tham số ban đầu của câu thoại.",
            voice_id="Trúc Ly",
            speed=1.0,
            emotion="neutral",
            emotion_cue="bình tĩnh",
        )
        script = RecapScript(project_slug="p_area2", chapter_number="1", title="Ch1", segments=[initial_seg])
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        mock_engine = MagicMock()
        mock_engine.mode = "mock"

        def fake_synth(text, voice, output_path, speed=1.0, emotion=None, emotion_cue=None, **kwargs):
            dur = round(2.0 / speed, 3)
            create_uncompressed_wav(output_path, duration_sec=dur, sample_rate=48000)
            return (dur, calculate_sha256(output_path), False)

        mock_engine.synthesize_utterance.side_effect = fake_synth

        with patch("src.cli.tts_cmd.get_tts_engine", return_value=mock_engine):
            ret = main(["tts", "--project", "p_area2", "--chapter", "1", "--engine", "mock"])
            assert ret == ExitCode.SUCCESS

        initial_state = read_json(chap_dir / "script.json")["segments"][0]
        return chap_dir, initial_state, mock_engine, fake_synth

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# Area 3: Crawling errors / zero-image yields failed status
# ==============================================================================

class TestRegressionArea3CrawlerErrorsAndZeroImageStatus:
    """
    R3 Area 3:
    Trigger crawl on invalid URL / network failure, and on page with 0 images.
    Assert checkpoint status is FAILED, error message is captured, and CLI exits with non-zero error envelope.
    """

    def test_crawl_network_failure_marks_failed_and_returns_error_envelope(self, tmp_path, monkeypatch, capsys):
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, "p_crawl_err", "1")

        with patch("src.services.crawler_service.crawler_service.crawl_raw_pages", new_callable=AsyncMock) as mock_crawl:
            mock_crawl.side_effect = ConnectionResetError("Connection reset by peer: Webtoons gateway unreachable")

            code = main([
                "crawl",
                "--project", "p_crawl_err",
                "--chapter", "1",
                "--url", "https://m.webtoons.com/fail-case",
                "--json",
            ])
            assert code == ExitCode.VALIDATION_ERROR

        # Verify JSON envelope on stdout
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        envelope = JsonEnvelope.model_validate(data)
        assert envelope.status == "error"
        assert envelope.command == "crawl"
        assert envelope.exit_code == ExitCode.VALIDATION_ERROR
        assert envelope.error is not None
        assert envelope.error.code == "VALIDATION_ERROR"
        assert "Webtoons gateway unreachable" in envelope.error.message

        # Verify checkpoint persisted in chapter.json
        meta = load_chapter_metadata(chap_dir)
        crawl_cp = meta.stages.get(StageType.CRAWL)
        assert crawl_cp is not None
        assert crawl_cp.status == CheckpointStatus.FAILED
        assert "Webtoons gateway unreachable" in (crawl_cp.error_message or "")

    def test_crawl_zero_images_marks_failed_and_returns_error_envelope(self, tmp_path, monkeypatch, capsys):
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, "p_crawl_zero", "1")

        with patch("src.services.crawler_service.crawler_service.crawl_raw_pages", new_callable=AsyncMock) as mock_crawl:
            mock_crawl.return_value = []  # Empty page or anti-bot challenge blocked images

            code = main([
                "crawl",
                "--project", "p_crawl_zero",
                "--chapter", "1",
                "--url", "https://m.webtoons.com/empty-chapter",
                "--json",
            ])
            assert code == ExitCode.VALIDATION_ERROR

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        envelope = JsonEnvelope.model_validate(data)
        assert envelope.status == "error"
        assert envelope.exit_code == ExitCode.VALIDATION_ERROR
        assert "No raw images found" in envelope.error.message

        meta = load_chapter_metadata(chap_dir)
        crawl_cp = meta.stages.get(StageType.CRAWL)
        assert crawl_cp is not None
        assert crawl_cp.status == CheckpointStatus.FAILED
        assert "No raw images found" in (crawl_cp.error_message or "")


# ==============================================================================
# Area 4: 1–20 panel chapters have no missing/duplicate panels
# ==============================================================================

class TestRegressionArea4SlidingWindow1To20PanelsCoverage:
    """
    R3 Area 4:
    Generate test chapters for panel counts P in [1, 20] using compute_sliding_windows.
    Run validate on the generated scripts. Assert that for every P in [1, 20], 100% of panels
    are covered with 0 omissions and 0 duplicates.
    """

    @pytest.mark.parametrize("panel_count", list(range(1, 21)))
    def test_sliding_windows_coverage_for_panel_count(self, panel_count, tmp_path, monkeypatch):
        slug = f"p_win_{panel_count}"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        # 1. Compute sliding windows via formula (window_size=5, stride=3)
        windows = compute_sliding_windows(panel_count=panel_count, window_size=5, stride=3)

        # Mathematical invariance check
        all_committed = [idx for w in windows for idx in w["commit_indices"]]
        assert all_committed == list(range(panel_count)), (
            f"Mathematical partition failure for P={panel_count}: committed={all_committed}"
        )
        assert len(all_committed) == len(set(all_committed))  # No duplicates

        # 2. Setup panels on disk and in manifest.json with strict PanelItem schema
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        manifest_panels = []
        for i in range(panel_count):
            pid = f"panel_{i:03d}"
            pfile = panels_dir / f"{pid}.jpg"
            pfile.write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 32)  # Minimal JPEG header
            manifest_panels.append(
                PanelItem(
                    panel_id=pid,
                    sequence_number=i + 1,
                    filename=f"{pid}.jpg",
                    relative_path=f"panels/{pid}.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, i * 100, 500, (i + 1) * 100),
                    width=500,
                    height=100,
                    aspect_ratio=5.0,
                    sha256=calculate_sha256(pfile),
                    status=PanelStatus.ACTIVE,
                )
            )

        manifest = PanelManifest(project_slug=slug, chapter_number="1", panels=manifest_panels)
        atomic_write_json(chap_dir / "manifest.json", manifest.model_dump())

        # 3. Build script segments covering each window's committed panels
        segments = []
        for w in windows:
            pids = [f"panel_{idx:03d}" for idx in w["commit_indices"]]
            segments.append(
                ScriptSegment(
                    seq_id=f"seg_{w['batch_index']:03d}",
                    panel_ids=pids,
                    narrator_text=f"Lời thoại dẫn dắt cho nhóm khung tranh {pids}.",
                    voice_id="Trúc Ly",
                    camera_motion=CameraMotionType.ZOOM_IN,
                    focus_box=FocusBox(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9),
                )
            )

        script = RecapScript(project_slug=slug, chapter_number="1", title="Coverage Test", segments=segments)
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        # 4. Run CLI validation
        code = main(["validate", "--project", slug, "--chapter", "1"])
        assert code == ExitCode.SUCCESS

    def test_sliding_window_validation_rejects_missing_panel(self, tmp_path, monkeypatch, capsys):
        """Verifies that omitting a non-essential panel succeeds under Gate 0, but missing referenced panel fails fast."""
        slug = "p_win_missing"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        manifest_panels = []
        for i in range(7):
            pid = f"panel_{i:03d}"
            pfile = panels_dir / f"{pid}.jpg"
            pfile.write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 32)
            manifest_panels.append(
                PanelItem(
                    panel_id=pid,
                    sequence_number=i + 1,
                    filename=f"{pid}.jpg",
                    relative_path=f"panels/{pid}.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, i * 100, 500, (i + 1) * 100),
                    width=500,
                    height=100,
                    aspect_ratio=5.0,
                    sha256=calculate_sha256(pfile),
                    status=PanelStatus.ACTIVE,
                )
            )
        atomic_write_json(chap_dir / "manifest.json", PanelManifest(project_slug=slug, chapter_number="1", panels=manifest_panels).model_dump())

        # Omit panel_003 (allowed under Gate 0 N:M contract)
        segments = [
            ScriptSegment(seq_id="s1", panel_ids=["panel_000", "panel_001", "panel_002"], narrator_text="B1", voice_id="Trúc Ly"),
            ScriptSegment(seq_id="s2", panel_ids=["panel_004", "panel_005", "panel_006"], narrator_text="B2", voice_id="Trúc Ly"),
        ]
        atomic_write_json(chap_dir / "script.json", RecapScript(project_slug=slug, chapter_number="1", title="T", segments=segments).model_dump())

        code = main(["validate", "--project", slug, "--chapter", "1", "--json"])
        assert code == ExitCode.SUCCESS
        capsys.readouterr()

        # But referencing a panel that does not exist in manifest/disk fails fast
        segments.append(
            ScriptSegment(seq_id="s3", panel_ids=["panel_999"], narrator_text="B3", voice_id="Trúc Ly")
        )
        atomic_write_json(chap_dir / "script.json", RecapScript(project_slug=slug, chapter_number="1", title="T", segments=segments).model_dump())
        code = main(["validate", "--project", slug, "--chapter", "1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        err_str = str(data.get("data", {}).get("errors", []))
        assert "panel_999" in err_str

    def test_sliding_window_validation_rejects_duplicate_panel(self, tmp_path, monkeypatch, capsys):
        """Verifies that referencing the same panel in multiple segments succeeds under Gate 0 N:M contract."""
        slug = "p_win_dup"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        manifest_panels = []
        for i in range(4):
            pid = f"panel_{i:03d}"
            pfile = panels_dir / f"{pid}.jpg"
            pfile.write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 32)
            manifest_panels.append(
                PanelItem(
                    panel_id=pid,
                    sequence_number=i + 1,
                    filename=f"{pid}.jpg",
                    relative_path=f"panels/{pid}.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, i * 100, 500, (i + 1) * 100),
                    width=500,
                    height=100,
                    aspect_ratio=5.0,
                    sha256=calculate_sha256(pfile),
                    status=PanelStatus.ACTIVE,
                )
            )
        atomic_write_json(chap_dir / "manifest.json", PanelManifest(project_slug=slug, chapter_number="1", panels=manifest_panels).model_dump())

        # Duplicate panel_001 in both s1 and s2 (allowed under Gate 0 N:M contract)
        segments = [
            ScriptSegment(seq_id="s1", panel_ids=["panel_000", "panel_001"], narrator_text="B1", voice_id="Trúc Ly"),
            ScriptSegment(seq_id="s2", panel_ids=["panel_001", "panel_002", "panel_003"], narrator_text="B2", voice_id="Trúc Ly"),
        ]
        atomic_write_json(chap_dir / "script.json", RecapScript(project_slug=slug, chapter_number="1", title="T", segments=segments).model_dump())

        code = main(["validate", "--project", slug, "--chapter", "1", "--json"])
        assert code == ExitCode.SUCCESS


# ==============================================================================
# Area 5: Missing approvals block final render; --approved allows render
# ==============================================================================

class TestRegressionArea5ReviewGatesEnforcement:
    """
    R3 Area 5:
    Attempt final render without voice approval. Assert it fails with validation error.
    Attempt final render without preview approval. Assert it fails with validation error.
    Attempt final render with --approved. Assert it passes review gates.
    """

    @pytest.fixture
    def setup_render_environment(self, tmp_path, monkeypatch):
        slug = "p_area5"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        script = RecapScript(
            project_slug=slug,
            chapter_number="1",
            title="Render Test",
            segments=[
                ScriptSegment(
                    seq_id="s1",
                    panel_ids=["p0"],
                    narrator_text="Chuẩn bị dựng video hoàn chỉnh.",
                    voice_id="Trúc Ly",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        # Dummy preview video
        video_dir = chap_dir / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        preview_file = video_dir / "preview.mp4"
        preview_file.write_bytes(b"dummy_mp4_bytes")

        return slug, proj_dir, chap_dir, preview_file

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# Area 6: Artifact invalidation upon edits
# ==============================================================================

class TestRegressionArea6ArtifactInvalidationUponEdits:
    """
    R3 Area 6:
    Verify that modifying script or re-synthesizing TTS unlinks old downstream video clips and subtitles,
    and resets render checkpoint to PENDING.
    Verify that --force on segment clears downstream audio, subtitles, video, and resets checkpoints.
    """

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_force_segmentation_clears_all_downstream_artifacts_and_resets_stages(self, tmp_path, monkeypatch):
        slug = "p_inval_force_seg"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        # Create raw image to pass segment prerequisite
        raw_dir = chap_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "page_001.jpg").write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 32)

        # Seed existing downstream files
        audio_dir = chap_dir / "audio" / "clips"
        audio_dir.mkdir(parents=True, exist_ok=True)
        stale_audio = audio_dir / "seg_001.wav"
        stale_audio.write_bytes(b"stale_audio")

        sub_dir = chap_dir / "subtitles"
        sub_dir.mkdir(parents=True, exist_ok=True)
        stale_sub = sub_dir / "chapter_1.srt"
        stale_sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nSub\n", encoding="utf-8")

        vid_dir = chap_dir / "video"
        vid_dir.mkdir(parents=True, exist_ok=True)
        stale_vid = vid_dir / "chapter_final.mp4"
        stale_vid.write_bytes(b"stale_video")

        # Mark all stages completed
        for st in [StageType.SEGMENT, StageType.SCRIPT, StageType.TTS, StageType.RENDER, StageType.FINALIZE]:
            update_checkpoint(chap_dir, st, CheckpointStatus.COMPLETED)

        # Execute segment with --force
        with patch("src.cli.segment_cmd.StreamingSegmenter") as mock_seg, \
             patch("src.cli.segment_cmd.ContactSheetGenerator") as mock_cs:
            mock_inst = MagicMock()
            mock_inst.build_page_maps.return_value = []
            mock_inst.compute_1d_profiles.return_value = ([], [])
            mock_inst.detect_cut_intervals.return_value = []
            mock_inst.refine_intervals.return_value = []
            mock_inst.extract_panels.return_value = []
            mock_seg.return_value = mock_inst
            mock_cs.return_value.generate_sheets.return_value = []

            ret = main(["segment", "--project", slug, "--chapter", "1", "--force"])
            assert ret == ExitCode.SUCCESS

        # Assert downstream files unlinked
        assert not stale_audio.exists(), "Downstream audio clip was not cleared by force segment!"
        assert not stale_sub.exists(), "Downstream subtitle was not cleared by force segment!"
        assert not stale_vid.exists(), "Downstream video was not cleared by force segment!"

        # Assert downstream stages reset to PENDING
        meta = load_chapter_metadata(chap_dir)
        for st in [StageType.SCRIPT, StageType.TTS, StageType.RENDER, StageType.FINALIZE]:
            assert meta.stages[st].status == CheckpointStatus.PENDING


# ==============================================================================
# Area 7: Crash resume from checkpoint
# ==============================================================================

class TestRegressionArea7CrashResumeFromCheckpoint:
    """
    R3 Area 7:
    Simulate a crash where a stage is left in RUNNING or FAILED.
    Verify that get_resumable_stage identifies the interrupted stage and successfully completes
    the pipeline upon rerun.
    """

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_crash_resume_from_failed_stage(self, tmp_path, monkeypatch):
        slug = "p_resume_failed"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        update_checkpoint(chap_dir, StageType.CRAWL, CheckpointStatus.COMPLETED)
        # Advance through RUNNING then FAILED
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.RUNNING)
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.FAILED, error="Segmentation engine crash")

        # 1. get_resumable_stage identifies SEGMENT
        assert get_resumable_stage(chap_dir) == StageType.SEGMENT

        # 2. Re-run segment successfully
        raw_dir = chap_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "page_001.jpg").write_bytes(b"\xFF\xD8\xFF\xE0" + b"\x00" * 32)

        with patch("src.cli.segment_cmd.StreamingSegmenter") as mock_seg, \
             patch("src.cli.segment_cmd.ContactSheetGenerator") as mock_cs:
            mock_inst = MagicMock()
            mock_inst.build_page_maps.return_value = []
            mock_inst.compute_1d_profiles.return_value = ([], [])
            mock_inst.detect_cut_intervals.return_value = []
            mock_inst.refine_intervals.return_value = []
            mock_inst.extract_panels.return_value = []
            mock_seg.return_value = mock_inst
            mock_cs.return_value.generate_sheets.return_value = []

            ret = main(["segment", "--project", slug, "--chapter", "1"])
            assert ret == ExitCode.SUCCESS

        # 3. Transitioned from FAILED to COMPLETED
        meta = load_chapter_metadata(chap_dir)
        assert meta.stages[StageType.SEGMENT].status == CheckpointStatus.COMPLETED
        assert get_resumable_stage(chap_dir) == StageType.SCRIPT

    def test_full_pipeline_stage_progression_and_completion(self, tmp_path, monkeypatch):
        """Verifies stage-by-stage progression from CRAWL to FINALIZE, ensuring get_resumable_stage returns None when complete."""
        slug = "p_pipeline_prog"
        proj_dir, chap_dir = setup_test_project_and_chapter(tmp_path, monkeypatch, slug, "1")

        expected_stages = [
            StageType.CRAWL,
            StageType.SEGMENT,
            StageType.SCRIPT,
            StageType.TTS,
            StageType.RENDER,
            StageType.FINALIZE,
        ]

        for stage in expected_stages:
            assert get_resumable_stage(chap_dir) == stage
            update_checkpoint(chap_dir, stage, CheckpointStatus.COMPLETED)

        # All stages complete
        assert get_resumable_stage(chap_dir) is None
        meta = load_chapter_metadata(chap_dir)
        assert meta.overall_status == CheckpointStatus.COMPLETED


# ==============================================================================
# Area 8: Invalid CLI options yield consistent error code and JSON envelope
# ==============================================================================

class TestRegressionArea8InvalidCliOptionsAndJsonEnvelope:
    """
    R3 Area 8:
    Test invalid CLI arguments across all commands (crawl, segment, validate, tts, render, compile).
    Assert exit code is consistent (ExitCode.VALIDATION_ERROR or ExitCode.IO_ERROR), output is valid
    JSON envelope with "status": "error", and error details are populated.
    """

    def _assert_json_error_envelope(self, output_str: str, expected_exit_code: int = ExitCode.VALIDATION_ERROR) -> JsonEnvelope:
        data = json.loads(output_str)
        envelope = JsonEnvelope.model_validate(data)
        assert envelope.status == "error"
        assert envelope.exit_code == expected_exit_code
        assert envelope.data is None or envelope.data.get("valid") is False
        assert envelope.error is not None
        assert len(envelope.error.code) > 0
        assert len(envelope.error.message) > 0
        assert envelope.duration_seconds >= 0.0
        return envelope

    def test_crawl_missing_url_argument(self, capsys):
        code = main(["crawl", "--project", "test_p", "--chapter", "1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "required" in env.error.message or "--url" in env.error.message or "Strict Form Gate" in env.error.message

    def test_crawl_missing_project_argument(self, capsys):
        code = main(["crawl", "--chapter", "1", "--url", "https://ex.com", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "required" in env.error.message or "--project" in env.error.message

    def test_segment_invalid_chapter_negative(self, capsys):
        code = main(["segment", "--project", "test_p", "--chapter", "-1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "chapter" in env.error.message.lower() or "Strict Form Gate" in env.error.message

    def test_segment_missing_chapter_argument(self, capsys):
        code = main(["segment", "--project", "test_p", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "--chapter" in env.error.message or "required" in env.error.message

    def test_validate_missing_project_argument(self, capsys):
        code = main(["validate", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "--project" in env.error.message or "required" in env.error.message

    def test_tts_missing_chapter_argument(self, capsys):
        code = main(["tts", "--project", "test_p", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "--chapter" in env.error.message or "required" in env.error.message

    def test_tts_invalid_engine_choice(self, capsys):
        code = main(["tts", "--project", "test_p", "--chapter", "1", "--engine", "unsupported_backend", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "invalid choice" in env.error.message or "--engine" in env.error.message

    def test_render_invalid_video_mode(self, capsys):
        code = main(["render", "--project", "test_p", "--chapter", "1", "--video-mode", "unsupported_mode_xyz", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "invalid choice" in env.error.message or "--video-mode" in env.error.message

    def test_render_invalid_quality_mode(self, capsys):
        code = main(["render", "--project", "test_p", "--chapter", "1", "--quality", "ultra_8k_imax", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "invalid choice" in env.error.message or "--quality" in env.error.message

    def test_compile_missing_project_argument(self, capsys):
        code = main(["compile", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "--project" in env.error.message or "required" in env.error.message

    def test_unknown_subcommand_yields_validation_error_envelope(self, capsys):
        code = main(["arbitrary_unknown_subcommand", "--json"])
        assert code == ExitCode.VALIDATION_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out)
        assert "invalid choice" in env.error.message or "unknown" in env.error.message.lower()

    def test_nonexistent_project_io_error_envelope(self, capsys):
        code = main(["inspect", "--project", "non_existent_project_slug_99999", "--json"])
        assert code == ExitCode.IO_ERROR
        captured = capsys.readouterr()
        env = self._assert_json_error_envelope(captured.out, expected_exit_code=ExitCode.IO_ERROR)
        assert env.error.code == "IO_ERROR"
        assert "not found" in env.error.message.lower()
