"""
tests/unit/test_p0_fixes.py
Comprehensive unit and regression tests verifying P0 fixes FIX-01 through FIX-05:
- FIX-01: TTS resume & audio fingerprinting
- FIX-02: Crawler exception handling & failed checkpoint marking
- FIX-03: Sliding window consistency & panel coverage validation
- FIX-04: Review gates enforcement & explicit --approved flag
- FIX-05: CLI arguments sync (--video-mode, segment --force, compile -ar 48000)
"""

from v4_fixtures import init_confirmed_project

import argparse
import asyncio
import json
import math
import struct
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
import pytest

from src.cli.exit_codes import ExitCode, CliValidationError
from src.cli.main import build_parser, main
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    Manifest,
    PanelItem,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    ScriptSegment,
    StageType,
)
from src.core.storage import (
    StorageFileNotFoundError,
    StorageManager,
    atomic_write_json,
    calculate_sha256,
    calculate_utterance_fingerprint,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    load_manifest,
    save_manifest,
    update_checkpoint,
)
from src.core.utils import compute_sliding_windows
from src.render.video_engine import VideoEngine, VideoMode


# ============================================================================
# FIX-01: TTS Resume & Audio Fingerprinting Tests
# ============================================================================

class TestFix01TtsFingerprint:
    """Tests deterministic calculation and sensitivity of utterance fingerprints."""

    def test_fingerprint_deterministic(self):
        fp1 = calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        fp2 = calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex string

    def test_fingerprint_sensitive_to_all_parameters(self):
        base_fp = calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        # Changing text
        assert base_fp != calculate_utterance_fingerprint(
            text="Xin chào các bạn",
            voice="vie_male_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        # Changing voice
        assert base_fp != calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_female_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        # Changing speed
        assert base_fp != calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.1,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        # Changing emotion
        assert base_fp != calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.0,
            emotion="tense",
            emotion_cue="calm",
            engine_mode="vieneu_local",
        )
        # Changing emotion cue
        assert base_fp != calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="whisper",
            engine_mode="vieneu_local",
        )
        # Changing engine mode
        assert base_fp != calculate_utterance_fingerprint(
            text="Xin chào",
            voice="vie_male_01",
            speed=1.0,
            emotion="neutral",
            emotion_cue="calm",
            engine_mode="mock_engine",
        )

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ============================================================================
# FIX-02: Crawler Exception Handling Tests
# ============================================================================

class TestFix02CrawlerExceptionHandling:
    """Verifies that crawl failures mark FAILED checkpoint and exit non-zero."""

    def test_crawler_marks_failed_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        # Mock crawler_service to raise connection error
        with patch("src.services.crawler_service.crawler_service.crawl_raw_pages", new_callable=AsyncMock) as mock_crawl:
            mock_crawl.side_effect = ConnectionError("Network unreachable")

            code = main(["crawl", "--project", "p1", "--chapter", "1", "--url", "https://example.com/chap1"])
            assert code == ExitCode.VALIDATION_ERROR

        cp = load_chapter_metadata(chap_dir).stages.get(StageType.CRAWL)
        assert cp is not None
        assert cp.status == CheckpointStatus.FAILED
        assert "Network unreachable" in (cp.error_message or "")

    def test_crawler_marks_failed_on_zero_images(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        with patch("src.services.crawler_service.crawler_service.crawl_raw_pages", new_callable=AsyncMock) as mock_crawl:
            mock_crawl.return_value = []  # 0 images downloaded

            code = main(["crawl", "--project", "p1", "--chapter", "1", "--url", "https://example.com/empty"])
            assert code == ExitCode.VALIDATION_ERROR

        cp = load_chapter_metadata(chap_dir).stages.get(StageType.CRAWL)
        assert cp is not None
        assert cp.status == CheckpointStatus.FAILED


# ============================================================================
# FIX-03: Sliding Window & Coverage Tests
# ============================================================================

class TestFix03SlidingWindowConsistency:
    """Verifies stride 3 sliding window partitioning and 100% panel coverage validation."""

    def test_compute_sliding_windows_partitioning_1_to_20(self):
        for p_count in range(1, 21):
            windows = compute_sliding_windows(p_count, window_size=5, stride=3)
            assert len(windows) > 0

            committed_panels = []
            for k, win in enumerate(windows):
                assert win["batch_index"] == k
                com_start = 3 * k
                com_end = min(3 * k + 3, p_count)
                look_start = 3 * k
                look_end = min(3 * k + 5, p_count)

                assert win["commit_indices"] == list(range(com_start, com_end))
                assert win["window_indices"] == list(range(look_start, look_end))
                committed_panels.extend(win["commit_indices"])

            # Exactly covers 0 to p_count - 1 with no duplicates
            assert committed_panels == list(range(p_count))

    def test_validate_cmd_detects_missing_panels(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        manifest = PanelManifest(
            project_slug="p1",
            chapter_number="1",
            panels=[
                PanelItem(
                    panel_id="panel_000",
                    sequence_number=1,
                    filename="panel_000.jpg",
                    relative_path="panels/panel_000.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, 0, 100, 100),
                    width=100,
                    height=100,
                    aspect_ratio=1.0,
                    sha256="0" * 64,
                    status=PanelStatus.ACTIVE,
                ),
                PanelItem(
                    panel_id="panel_001",
                    sequence_number=2,
                    filename="panel_001.jpg",
                    relative_path="panels/panel_001.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, 100, 100, 200),
                    width=100,
                    height=100,
                    aspect_ratio=1.0,
                    sha256="1" * 64,
                    status=PanelStatus.ACTIVE,
                ),
                PanelItem(
                    panel_id="panel_002",
                    sequence_number=3,
                    filename="panel_002.jpg",
                    relative_path="panels/panel_002.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, 200, 100, 300),
                    width=100,
                    height=100,
                    aspect_ratio=1.0,
                    sha256="2" * 64,
                    status=PanelStatus.ACTIVE,
                ),
            ],
        )
        atomic_write_json(chap_dir / "manifest.json", manifest.model_dump())

        # Script covers only panel_000 and panel_001 (panel_002 is missing)
        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_000"], narrator_text="Đoạn một"),
                ScriptSegment(seq_id="002", panel_ids=["panel_001"], narrator_text="Đoạn hai"),
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        code = main(["validate", "--project", "p1", "--chapter", "1"])
        assert code == ExitCode.VALIDATION_ERROR

    def test_validate_cmd_detects_duplicate_panels(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        manifest = PanelManifest(
            project_slug="p1",
            chapter_number="1",
            panels=[
                PanelItem(
                    panel_id="panel_000",
                    sequence_number=1,
                    filename="panel_000.jpg",
                    relative_path="panels/panel_000.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, 0, 100, 100),
                    width=100,
                    height=100,
                    aspect_ratio=1.0,
                    sha256="0" * 64,
                    status=PanelStatus.ACTIVE,
                ),
                PanelItem(
                    panel_id="panel_001",
                    sequence_number=2,
                    filename="panel_001.jpg",
                    relative_path="panels/panel_001.jpg",
                    source_image="page_001.jpg",
                    crop_box=(0, 100, 100, 200),
                    width=100,
                    height=100,
                    aspect_ratio=1.0,
                    sha256="1" * 64,
                    status=PanelStatus.ACTIVE,
                ),
            ],
        )
        atomic_write_json(chap_dir / "manifest.json", manifest.model_dump())

        # Script duplicates panel_000 across multiple segments
        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_000"], narrator_text="Đoạn một"),
                ScriptSegment(seq_id="002", panel_ids=["panel_000", "panel_001"], narrator_text="Đoạn hai"),
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        code = main(["validate", "--project", "p1", "--chapter", "1"])
        assert code == ExitCode.VALIDATION_ERROR


# ============================================================================
# FIX-04: Review Gates Enforcement Tests
# ============================================================================

class TestFix04ReviewGatesEnforcement:
    """Verifies that final render strictly requires Gate 1 & 2 approvals."""

    def _setup_script(self, chap_dir: Path):
        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_final_render_rejected_without_voice_approval(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "final"])
        assert code == ExitCode.VALIDATION_ERROR

    def test_final_render_rejects_skip_review_bypass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "final", "--skip-review"])
        assert code == ExitCode.VALIDATION_ERROR

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ============================================================================
# FIX-05: CLI Arguments & Sync Tests
# ============================================================================

class TestFix05CliArgumentsAndSync:
    """Verifies CLI argument forwarding, validation, and audio sample rate."""

    def test_video_mode_choices_enforced(self):
        parser = build_parser()
        # Valid choices parse cleanly
        for mode in ["auto", "landscape_fit", "portrait_vertical", "portrait_padded"]:
            args = parser.parse_args(["render", "--project", "p1", "--chapter", "1", "--video-mode", mode])
            assert args.video_mode == mode

        # Invalid choice raises SystemExit (argparse validation)
        with pytest.raises(SystemExit):
            parser.parse_args(["render", "--project", "p1", "--chapter", "1", "--video-mode", "invalid_mode"])

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    def test_segment_cmd_respects_force_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_confirmed_project("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        # Provide a raw image
        raw_img = chap_dir / "raw" / "page_001.jpg"
        raw_img.parent.mkdir(parents=True, exist_ok=True)
        raw_img.write_bytes(b"dummy_image_data")

        # Mark SEGMENT as completed and create dummy manifest
        update_checkpoint(chap_dir, StageType.SEGMENT, CheckpointStatus.COMPLETED)
        manifest = PanelManifest(
            project_slug="p1",
            chapter_number="1",
            panels=[],
        )
        atomic_write_json(chap_dir / "manifest.json", manifest.model_dump())

        # Without force: should return cached manifest without re-running segmentation
        with patch("src.cli.segment_cmd.StreamingSegmenter") as mock_seg_cls:
            code = main(["segment", "--project", "p1", "--chapter", "1"])
            assert code == 0
            assert mock_seg_cls.call_count == 0

        # With force: should invoke StreamingSegmenter
        with patch("src.cli.segment_cmd.StreamingSegmenter") as mock_seg_cls, \
             patch("src.cli.segment_cmd.ContactSheetGenerator") as mock_cs_cls:
            mock_inst = MagicMock()
            mock_inst.build_page_maps.return_value = []
            mock_inst.compute_1d_profiles.return_value = ([], [])
            mock_inst.detect_cut_intervals.return_value = []
            mock_inst.refine_intervals.return_value = []
            mock_inst.extract_panels.return_value = []
            mock_seg_cls.return_value = mock_inst

            mock_cs = MagicMock()
            mock_cs.generate_sheets.return_value = []
            mock_cs_cls.return_value = mock_cs

            code = main(["segment", "--project", "p1", "--chapter", "1", "--force"])
            assert code == 0
            assert mock_seg_cls.call_count == 1

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ============================================================================
# FIX-06: P0 Core Correctness Regression Tests
# ============================================================================

def make_test_wav(path: Path, duration_sec: float = 1.5, sample_rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = bytearray()
        for i in range(num_samples):
            v = int(8000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            raw.extend(struct.pack("<h", v))
        wf.writeframes(raw)
    return path


def make_test_image(path: Path, width: int = 800, height: int = 1200, color=(100, 150, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(path, "JPEG")
    return path


class TestFix06P0CoreCorrectness:
    """Regression test suite for P0 multi-panel rendering, failure handling, cache integrity, and drift."""

    def test_manifest_storage_helpers(self, tmp_path):
        """Verifies load_manifest and save_manifest on storage and StorageManager."""
        assert load_manifest(tmp_path / "non_existent") is None

        m = PanelManifest(
            project_slug="demo_p",
            chapter_number="1",
            panels=[
                PanelItem(
                    panel_id="p001",
                    sequence_number=1,
                    filename="p001.jpg",
                    relative_path="panels/p001.jpg",
                    source_image="raw_01.jpg",
                    crop_box=(0, 0, 100, 100),
                    width=100,
                    height=100,
                    aspect_ratio=1.0,
                    sha256="abc123",
                )
            ],
        )
        saved_file = save_manifest(tmp_path, m)
        assert saved_file.exists()
        assert saved_file.name == "manifest.json"

        loaded = load_manifest(tmp_path)
        assert loaded is not None
        assert loaded.project_slug == "demo_p"
        assert len(loaded.panels) == 1
        assert loaded.panels[0].panel_id == "p001"

        # Facade StorageManager
        assert StorageManager.load_manifest(tmp_path).project_slug == "demo_p"
        m.project_slug = "demo_facade"
        StorageManager.save_manifest(tmp_path, m)
        assert load_manifest(tmp_path).project_slug == "demo_facade"

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
