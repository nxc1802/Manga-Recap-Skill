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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.cli.exit_codes import ExitCode, CliValidationError
from src.cli.main import build_parser, main
from src.core.schemas import (
    CheckpointStatus,
    PanelItem,
    PanelManifest,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    ScriptSegment,
    StageType,
)
from src.core.storage import (
    atomic_write_json,
    calculate_sha256,
    calculate_utterance_fingerprint,
    init_chapter_layout,
    init_project_layout,
    load_chapter_metadata,
    update_checkpoint,
)
from src.core.utils import compute_sliding_windows


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
