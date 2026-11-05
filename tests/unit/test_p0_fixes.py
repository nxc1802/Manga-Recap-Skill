"""
tests/unit/test_p0_fixes.py
Comprehensive unit and regression tests verifying P0 fixes FIX-01 through FIX-05:
- FIX-01: TTS resume & audio fingerprinting
- FIX-02: Crawler exception handling & failed checkpoint marking
- FIX-03: Sliding window consistency & panel coverage validation
- FIX-04: Review gates enforcement & explicit --approved flag
- FIX-05: CLI arguments sync (--video-mode, segment --force, compile -ar 48000)
"""

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

    def test_tts_cmd_resumes_matching_and_resynthesizes_on_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        # Create valid script
        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_000"],
                    narrator_text="Câu chuyện bắt đầu tại một thành phố cổ kính.",
                    voice_id="vie_male_01",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        # Synthesize first time
        mock_engine = MagicMock()
        mock_engine.mode = "mock"

        def fake_synth(text, voice, output_path, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"RIFF" + b"\x00" * 200)
            return (2.0, calculate_sha256(output_path), False)

        mock_engine.synthesize_utterance.side_effect = fake_synth

        with patch("src.cli.tts_cmd.get_tts_engine", return_value=mock_engine):
            ret = main(["tts", "--project", "p1", "--chapter", "1", "--engine", "mock"])
            assert ret == 0
            assert mock_engine.synthesize_utterance.call_count == 1

        # Check script.json now has fingerprint and checksum
        with open(chap_dir / "script.json") as f:
            saved_script = json.load(f)
        saved_seg = saved_script["segments"][0]
        assert saved_seg.get("fingerprint") is not None
        assert saved_seg.get("checksum") is not None

        # Run tts second time: should reuse without calling synthesize_utterance
        mock_engine.synthesize_utterance.reset_mock()
        with patch("src.cli.tts_cmd.get_tts_engine", return_value=mock_engine):
            ret = main(["tts", "--project", "p1", "--chapter", "1", "--engine", "mock"])
            assert ret == 0
            assert mock_engine.synthesize_utterance.call_count == 0

        # Change narration in script: fingerprint mismatch triggers re-synthesis
        saved_script["segments"][0]["narrator_text"] = "Câu chuyện bắt đầu tại vương quốc tuyết rơi."
        atomic_write_json(chap_dir / "script.json", saved_script)

        mock_engine.synthesize_utterance.reset_mock()
        with patch("src.cli.tts_cmd.get_tts_engine", return_value=mock_engine):
            ret = main(["tts", "--project", "p1", "--chapter", "1", "--engine", "mock"])
            assert ret == 0
            assert mock_engine.synthesize_utterance.call_count == 1


# ============================================================================
# FIX-02: Crawler Exception Handling Tests
# ============================================================================

class TestFix02CrawlerExceptionHandling:
    """Verifies that crawl failures mark FAILED checkpoint and exit non-zero."""

    def test_crawler_marks_failed_on_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
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

        proj_dir = init_project_layout("p1", "Test Project")
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

        proj_dir = init_project_layout("p1", "Test Project")
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

        proj_dir = init_project_layout("p1", "Test Project")
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

    def test_preview_render_does_not_require_gates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        with patch("src.cli.render_cmd.VideoEngine") as mock_engine_cls:
            mock_inst = MagicMock()
            mock_engine_cls.return_value = mock_inst
            code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "preview"])
            assert code == 0

    def test_final_render_rejected_without_voice_approval(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "final"])
        assert code == ExitCode.VALIDATION_ERROR

    def test_final_render_rejects_skip_review_bypass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "final", "--skip-review"])
        assert code == ExitCode.VALIDATION_ERROR

    def test_final_render_succeeds_with_approved_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        with patch("src.cli.render_cmd.VideoEngine") as mock_engine_cls:
            mock_inst = MagicMock()
            mock_engine_cls.return_value = mock_inst
            code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "final", "--approved"])
            assert code == 0

    def test_final_render_succeeds_with_both_approvals_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        self._setup_script(chap_dir)

        # Gate 1 approval
        voice_sel = {
            "strategy": "multi_character",
            "primary_voice": "Trúc Ly",
            "reviewer": "lead_director",
            "voice_mapping": {"main": "vie_male_01"},
            "approved": True,
            "notes": "Tone verified",
        }
        atomic_write_json(proj_dir / "voice_selection.json", voice_sel)

        # Gate 2 preview and approval
        preview_file = chap_dir / "video" / "chapter_preview.mp4"
        preview_file.parent.mkdir(parents=True, exist_ok=True)
        preview_file.write_bytes(b"dummy_mp4_preview")

        preview_app = {
            "chapter": 1,
            "approved": True,
            "reviewer": "qa_lead",
            "pacing_score": 9,
            "emotion_score": 9,
        }
        atomic_write_json(chap_dir / "preview_approval.json", preview_app)

        with patch("src.cli.render_cmd.VideoEngine") as mock_engine_cls:
            mock_inst = MagicMock()
            mock_engine_cls.return_value = mock_inst
            code = main(["render", "--project", "p1", "--chapter", "1", "--quality", "final"])
            assert code == 0


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

    def test_render_forwards_video_mode_to_engine(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")
        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        with patch("src.cli.render_cmd.VideoEngine") as mock_engine_cls:
            mock_inst = MagicMock()
            mock_engine_cls.return_value = mock_inst

            code = main([
                "render", "--project", "p1", "--chapter", "1",
                "--quality", "preview", "--video-mode", "portrait_vertical",
            ])
            assert code == 0
            assert mock_inst.render_chapter.call_args.kwargs.get("video_mode") == "portrait_vertical"

    def test_segment_cmd_respects_force_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
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

    def test_compile_cmd_uses_48khz_audio_extraction(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        ch1_dir = init_chapter_layout("p1", "1")

        v1 = ch1_dir / "video" / "chapter_final.mp4"
        v1.parent.mkdir(parents=True, exist_ok=True)
        v1.write_bytes(b"dummy")

        def fake_ffmpeg(cmd, *args, **kwargs):
            out_mp4 = proj_dir / "video" / "final.mp4"
            out_mp4.parent.mkdir(parents=True, exist_ok=True)
            out_mp4.write_bytes(b"final_video_bytes")
            out_wav = proj_dir / "audio" / "final.wav"
            out_wav.parent.mkdir(parents=True, exist_ok=True)
            out_wav.write_bytes(b"final_audio_bytes")
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_ffmpeg) as mock_run:
            code = main(["compile", "--project", "p1", "--chapters", "1", "--approved"])
            assert code == 0

            # Find the ffmpeg call that extracts audio and verify "-ar", "48000"
            audio_extract_calls = [
                c[0][0] for c in mock_run.call_args_list
                if isinstance(c[0][0], list) and "-vn" in c[0][0]
            ]
            assert len(audio_extract_calls) > 0
            cmd = audio_extract_calls[0]
            assert "-ar" in cmd
            ar_idx = cmd.index("-ar")
            assert cmd[ar_idx + 1] == "48000"


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

    def test_manifest_relative_path_resolution(self, tmp_path, monkeypatch):
        """Verifies that panels are resolved via manifest.json relative_path without guessing filenames."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        # Create panel with custom filename and path
        custom_art = chap_dir / "panels" / "custom_hero_art.png"
        make_test_image(custom_art, width=640, height=480, color=(200, 40, 40))

        # Register in manifest.json with panel_id="panel_hero"
        manifest = PanelManifest(
            project_slug="p1",
            chapter_number="1",
            panels=[
                PanelItem(
                    panel_id="panel_hero",
                    sequence_number=1,
                    filename="custom_hero_art.png",
                    relative_path="panels/custom_hero_art.png",
                    source_image="raw.jpg",
                    crop_box=(0, 0, 640, 480),
                    width=640,
                    height=480,
                    aspect_ratio=640 / 480,
                    sha256=calculate_sha256(custom_art),
                )
            ],
        )
        save_manifest(chap_dir, manifest)

        # Audio and script referencing "panel_hero"
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.2)
        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_hero"],
                    narrator_text="Anh hùng thức tỉnh.",
                    duration=1.2,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine()
        engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=True)

        clip_p = chap_dir / "video" / "clips" / "clip_preview_001.mp4"
        meta_p = chap_dir / "video" / "clips" / "clip_preview_001.meta.json"
        assert clip_p.exists()
        assert meta_p.exists()

        meta_data = json.loads(meta_p.read_text(encoding="utf-8"))
        assert meta_data["panel_hash"] == calculate_sha256(custom_art)

    def test_multi_panel_segment_frame_distribution(self, tmp_path, monkeypatch):
        """Verifies integer frame distribution across 3 panels in a single segment with 0 frame drift."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        p1 = make_test_image(chap_dir / "panels" / "p1.jpg", 640, 480, color=(10, 20, 30))
        p2 = make_test_image(chap_dir / "panels" / "p2.jpg", 640, 480, color=(40, 50, 60))
        p3 = make_test_image(chap_dir / "panels" / "p3.jpg", 640, 480, color=(70, 80, 90))

        # Audio is 3.0s -> 90 frames at 30 fps
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=3.0)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["p1.jpg", "p2.jpg", "p3.jpg"],
                    narrator_text="Chuỗi hành động liên hoàn qua ba khung tranh.",
                    duration=3.0,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine(fps=30)
        engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=True)

        # 3 clips must exist for segment 001
        c0 = chap_dir / "video" / "clips" / "clip_preview_001_0.mp4"
        c1 = chap_dir / "video" / "clips" / "clip_preview_001_1.mp4"
        c2 = chap_dir / "video" / "clips" / "clip_preview_001_2.mp4"
        assert c0.exists()
        assert c1.exists()
        assert c2.exists()

        m0 = json.loads(c0.with_suffix(".meta.json").read_text(encoding="utf-8"))
        m1 = json.loads(c1.with_suffix(".meta.json").read_text(encoding="utf-8"))
        m2 = json.loads(c2.with_suffix(".meta.json").read_text(encoding="utf-8"))

        assert m0["duration_ms"] == 1000  # 30 frames
        assert m1["duration_ms"] == 1000  # 30 frames
        assert m2["duration_ms"] == 1000  # 30 frames
        assert m0["duration_ms"] + m1["duration_ms"] + m2["duration_ms"] == 3000

        # Prime frame math verification: 91 frames across 3 panels -> 31, 30, 30 (sum = 91)
        f_total = 91
        n = 3
        f_base = f_total // n
        rem = f_total % n
        allocated = [f_base + (1 if k < rem else 0) for k in range(n)]
        assert allocated == [31, 30, 30]
        assert sum(allocated) == 91

    def test_multi_panel_audio_offset_no_repetition(self, tmp_path, monkeypatch):
        """Verifies that sliced audio offsets prevent speech from repeating on panel transitions."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        for name in ["p1.jpg", "p2.jpg", "p3.jpg"]:
            make_test_image(chap_dir / "panels" / name, 640, 480)

        # 6.0s audio for segment
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=6.0)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["p1.jpg", "p2.jpg", "p3.jpg"],
                    narrator_text="Đoạn thoại dài trải qua ba khung hình liên tiếp.",
                    duration=6.0,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine(fps=30)
        captured_calls = []
        original_gen = engine.generate_panel_clip

        def spy_generate(*args, **kwargs):
            captured_calls.append(kwargs)
            return original_gen(*args, **kwargs)

        with patch.object(engine, "generate_panel_clip", side_effect=spy_generate):
            engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=True)

        assert len(captured_calls) == 3
        # Clip 0 starts at offset 0.0s, Clip 1 at 2.0s, Clip 2 at 4.0s
        assert captured_calls[0]["audio_offset_sec"] == pytest.approx(0.0, abs=0.01)
        assert captured_calls[1]["audio_offset_sec"] == pytest.approx(2.0, abs=0.01)
        assert captured_calls[2]["audio_offset_sec"] == pytest.approx(4.0, abs=0.01)

    def test_panel_sequence_order_no_omissions_no_duplicates(self, tmp_path, monkeypatch):
        """Verifies all referenced panels across 1, 2, and 3-panel segments are rendered in exact sequence."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        for i in range(1, 7):
            make_test_image(chap_dir / "panels" / f"panel_{i:03d}.jpg", 640, 480)

        make_test_wav(chap_dir / "audio" / "clips" / "a1.wav", duration_sec=1.0)
        make_test_wav(chap_dir / "audio" / "clips" / "a2.wav", duration_sec=2.0)
        make_test_wav(chap_dir / "audio" / "clips" / "a3.wav", duration_sec=3.0)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", panel_ids=["panel_001.jpg"], narrator_text="Khung 1", duration=1.0, audio_path="audio/clips/a1.wav"),
                ScriptSegment(seq_id="002", panel_ids=["panel_002.jpg", "panel_003.jpg"], narrator_text="Khung 2 và 3", duration=2.0, audio_path="audio/clips/a2.wav"),
                ScriptSegment(seq_id="003", panel_ids=["panel_004.jpg", "panel_005.jpg", "panel_006.jpg"], narrator_text="Khung 4 5 6", duration=3.0, audio_path="audio/clips/a3.wav"),
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine(fps=30)
        captured_concat_list = []
        orig_concat = engine.concatenate_clips

        def spy_concat(clip_paths, out_path):
            captured_concat_list.extend(clip_paths)
            return orig_concat(clip_paths, out_path)

        with patch.object(engine, "concatenate_clips", side_effect=spy_concat):
            engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=True)

        assert len(captured_concat_list) == 6
        expected_names = [
            "clip_preview_001.mp4",
            "clip_preview_002_0.mp4",
            "clip_preview_002_1.mp4",
            "clip_preview_003_0.mp4",
            "clip_preview_003_1.mp4",
            "clip_preview_003_2.mp4",
        ]
        actual_names = [Path(p).name for p in captured_concat_list]
        assert actual_names == expected_names

    def test_render_failure_does_not_reuse_stale_clip_or_commit_meta(self, tmp_path, monkeypatch):
        """Verifies that render errors unlink stale clips, never write false metadata, and mark FAILED."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        make_test_image(chap_dir / "panels" / "panel_001.jpg", 640, 480)
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.5)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="Đoạn văn kiểm thử lỗi.",
                    duration=1.5,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        # Put a stale clip and meta on disk
        clip_p = chap_dir / "video" / "clips" / "clip_preview_001.mp4"
        meta_p = chap_dir / "video" / "clips" / "clip_preview_001.meta.json"
        clip_p.parent.mkdir(parents=True, exist_ok=True)
        clip_p.write_bytes(b"STALE_CLIP_BYTES_" * 1000)
        meta_p.write_text(json.dumps({"dummy": "old_cache"}), encoding="utf-8")

        engine = VideoEngine()

        # Mock generate_panel_clip to fail (return False)
        with patch.object(engine, "generate_panel_clip", return_value=False):
            with pytest.raises(RuntimeError) as exc_info:
                engine.render_chapter(proj_dir, "1", quality="preview", force=True)
            assert "Failed to generate clip" in str(exc_info.value)

        # Assert stale clip and meta are deleted
        assert not clip_p.exists()
        assert not meta_p.exists()

        # Assert RENDER stage is marked FAILED in chapter metadata
        chap_meta = load_chapter_metadata(chap_dir)
        render_cp = chap_meta.stages.get(StageType.RENDER)
        assert render_cp is not None
        assert render_cp.status == CheckpointStatus.FAILED
        assert "Failed to generate clip" in str(render_cp.error_message)

    def test_image_modification_invalidates_cache(self, tmp_path, monkeypatch):
        """Verifies that changing a panel image invalidates cache and triggers re-rendering."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        p_file = make_test_image(chap_dir / "panels" / "panel_001.jpg", 640, 480, color=(100, 100, 100))
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.5)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="Kiểm tra bộ nhớ đệm hình ảnh.",
                    duration=1.5,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine()
        engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=True, force=False)

        clip_p = chap_dir / "video" / "clips" / "clip_preview_001.mp4"
        meta_p = chap_dir / "video" / "clips" / "clip_preview_001.meta.json"
        mtime_before = clip_p.stat().st_mtime_ns
        hash_before = json.loads(meta_p.read_text(encoding="utf-8"))["panel_hash"]

        # Modify panel image
        make_test_image(p_file, 640, 480, color=(255, 0, 0))
        assert calculate_sha256(p_file) != hash_before

        # Re-render without force: should detect panel_hash difference and re-render
        engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=True, force=False)

        mtime_after = clip_p.stat().st_mtime_ns
        hash_after = json.loads(meta_p.read_text(encoding="utf-8"))["panel_hash"]

        assert hash_after != hash_before
        assert mtime_after != mtime_before

    def test_disallow_placeholders_in_final_render(self, tmp_path, monkeypatch):
        """Verifies that final production renders strictly reject placeholder fallback."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        # Missing panel image
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.5)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["missing_panel.jpg"],
                    narrator_text="Thiếu ảnh trong render final.",
                    duration=1.5,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine()
        with pytest.raises(RuntimeError) as exc_info:
            engine.render_chapter(proj_dir, "1", quality="final", allow_placeholders=True)
        assert "disallowed in final production renders" in str(exc_info.value).lower()

    def test_unreadable_or_corrupt_panel_raises_error(self, tmp_path, monkeypatch):
        """Verifies that a 0-byte or corrupt panel image raises ValueError immediately."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        # Create 0-byte corrupted image
        bad_img = chap_dir / "panels" / "corrupt_panel.jpg"
        bad_img.parent.mkdir(parents=True, exist_ok=True)
        bad_img.write_bytes(b"")

        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.5)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["corrupt_panel.jpg"],
                    narrator_text="Hình ảnh lỗi 0 byte.",
                    duration=1.5,
                    audio_path="audio/clips/audio_1_001.wav",
                )
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine()
        with pytest.raises(ValueError) as exc_info:
            engine.render_chapter(proj_dir, "1", quality="preview")
        assert "empty (0 bytes)" in str(exc_info.value) or "corrupt" in str(exc_info.value).lower()

    def test_av_duration_drift_under_150ms(self, tmp_path, monkeypatch):
        """Verifies that rendered multi-panel video maintains audio/video timeline sync <= 150 ms."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))

        proj_dir = init_project_layout("p1", "Test Project")
        chap_dir = init_chapter_layout("p1", "1")

        make_test_image(chap_dir / "panels" / "p1.jpg", 640, 480)
        make_test_image(chap_dir / "panels" / "p2.jpg", 640, 480)
        make_test_image(chap_dir / "panels" / "p3.jpg", 640, 480)

        # 2 segments with total speech duration = 1.35 + 2.15 = 3.50s
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_001.wav", duration_sec=1.35)
        make_test_wav(chap_dir / "audio" / "clips" / "audio_1_002.wav", duration_sec=2.15)

        script = RecapScript(
            project_slug="p1",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["p1.jpg"],
                    narrator_text="Đoạn thoại một với một khung ảnh.",
                    duration=1.35,
                    audio_path="audio/clips/audio_1_001.wav",
                ),
                ScriptSegment(
                    seq_id="002",
                    panel_ids=["p2.jpg", "p3.jpg"],
                    narrator_text="Đoạn thoại hai với hai khung ảnh.",
                    duration=2.15,
                    audio_path="audio/clips/audio_1_002.wav",
                ),
            ],
        )
        atomic_write_json(chap_dir / "script.json", script.model_dump())

        engine = VideoEngine(fps=30)
        res = engine.render_chapter(proj_dir, "1", quality="preview", keep_temp=False)

        assert Path(res["video_path"]).exists()
        assert abs(res["duration_diff_seconds"]) <= 0.150
