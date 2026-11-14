"""
tests/unit/test_phase_c.py
Comprehensive Unit & Integration Test Suite for Phase C (PR-07: Single-Session Cloud Production & Persistent TTS + Render):
1. Persistent VieNeu-TTS Singleton Lifecycle, Thread Safety, and Telemetry
2. Hardware Encoder Probing, Smoke Testing, and Safe Fallback to libx264
3. Video Engine Render Cache Fingerprint Invalidation on Encoder Change
4. Production Job Manifest Schemas & Zero Secrets Guarantee
5. Single-Session Cloud Production Runner (4-stage pipeline, crash-resume, failure isolation, export bundle)
6. CLI Dispatch and Flag Synchronization (--encoder on render, batch, produce)
"""

from __future__ import annotations

from v4_fixtures import init_confirmed_project

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.cli.batch_cmd import handle_batch
from src.cli.main import build_parser, execute_subcommand
from src.cli.production_cmd import handle_produce
from src.cli.render_cmd import handle_render
from src.cli.tts_cmd import handle_tts
from src.core.schemas import (
    CameraMotionType,
    CheckpointStatus,
    FocusBox,
    ProjectConfig,
    RecapScript,
    RenderSettings,
    ScriptSegment,
    StageType,
)
from src.core.storage import (
    atomic_write_json,
    get_chapter_dir,
    get_output_dir,
    get_project_dir,
    init_chapter_layout,
    init_project_layout,
    read_json,
)
from src.production.hardware import (
    SUPPORTED_ENCODERS,
    get_encoder_codec_args,
    probe_available_encoders,
    resolve_encoder,
    resolve_video_encoder,
    smoke_test_encoder,
)
from src.production.runner import SingleSessionProductionRunner
from src.production.schemas import (
    ChapterProductionProgress,
    ProductionJobConfig,
    ProductionJobManifest,
    ProductionJobTelemetry,
    ProductionStageStatus,
)
from src.render.video_engine import VideoEngine
from src.tts.engine import EngineMode, MockTTSBackend, VieNeuEngine, get_tts_engine


# ==============================================================================
# Helper Fixtures
# ==============================================================================

@pytest.fixture(autouse=True)
def reset_tts_singleton():
    """Ensure clean TTS singleton state before and after each test."""
    VieNeuEngine.reset_instance()
    yield
    VieNeuEngine.reset_instance()


@pytest.fixture
def mock_cloud_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Initializes a self-contained test project with 2 sample chapters."""
    project_slug = "test_cloud_proj"
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MANGA_RECAP_OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr("src.core.utils.get_project_data_dir", lambda p: data_dir / "projects" / p)

    init_confirmed_project(project_slug, title="Test Cloud Project")
    p_dir = data_dir / "projects" / project_slug

    # Create two chapters: 1 and 2
    for c_num in [1, 2]:
        init_chapter_layout(project_slug, c_num)
        c_dir = p_dir / "chapters" / str(c_num)

        # Create dummy panel images
        panels_dir = c_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)
        img1 = panels_dir / "panel_001.jpg"
        img2 = panels_dir / "panel_002.jpg"
        Image.new("RGB", (800, 1200), color=(100, 150, 200)).save(img1, "JPEG")
        Image.new("RGB", (800, 1200), color=(120, 180, 220)).save(img2, "JPEG")

        # Create valid RecapScript
        script = RecapScript(
            project_slug=project_slug,
            title=f"Tập {c_num}",
            chapter_number=str(c_num),
            default_voice="Ngọc Huyền",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text=f"Bối cảnh mở đầu cho tập {c_num} câu chuyện recap.",
                    camera_motion=CameraMotionType.STATIC,
                    focus_box=FocusBox(x_min=0.1, y_min=0.1, x_max=0.8, y_max=0.8),
                ),
                ScriptSegment(
                    seq_id="002",
                    panel_ids=["panel_002.jpg"],
                    narrator_text=f"Diễn biến tiếp theo đầy gay cấn trong tập {c_num}.",
                    camera_motion=CameraMotionType.ZOOM_IN,
                    focus_box=FocusBox(x_min=0.2, y_min=0.2, x_max=0.9, y_max=0.9),
                ),
            ],
        )
        atomic_write_json(c_dir / "script.json", script.model_dump())

    yield {
        "project_slug": project_slug,
        "project_dir": p_dir,
        "data_dir": data_dir,
        "output_dir": output_dir,
    }


# ==============================================================================
# 1. Persistent TTS Singleton Lifecycle, Thread-Safety, and Telemetry Tests
# ==============================================================================

class TestTTSPersistentSingleton:
    def test_tts_singleton_reuses_instance_when_mode_matches(self):
        """Reusing get_tts_engine with same mode returns identical object without recreating."""
        e1 = get_tts_engine(force_mode=EngineMode.MOCK)
        e2 = get_tts_engine(force_mode=EngineMode.MOCK)
        assert e1 is e2, "VieNeuEngine singleton should reuse existing instance when mode matches"
        assert e1.mode == "mock"

    def test_tts_singleton_force_reload(self):
        """Passing force_reload=True explicitly recreates the singleton."""
        e1 = get_tts_engine(force_mode=EngineMode.MOCK)
        e2 = get_tts_engine(force_mode=EngineMode.MOCK, force_reload=True)
        assert e1 is not e2, "force_reload=True must instantiate a new VieNeuEngine object"

    def test_tts_singleton_reloads_when_mode_changes(self):
        """Switching mode from MOCK to NEURAL recreates instance."""
        e_mock = get_tts_engine(force_mode=EngineMode.MOCK)
        assert e_mock.mode == "mock"

        with patch("src.tts.engine.NeuralTTSBackend") as mock_neural_backend:
            mock_inst = MagicMock()
            mock_neural_backend.return_value = mock_inst
            e_neural = get_tts_engine(force_mode=EngineMode.NEURAL)
            assert e_neural is not e_mock
            assert e_neural.mode == "neural"

    def test_tts_singleton_reset_instance(self):
        """reset_instance() cleans up the singleton."""
        e1 = get_tts_engine(force_mode=EngineMode.MOCK)
        assert VieNeuEngine._instance is not None
        VieNeuEngine.reset_instance()
        assert VieNeuEngine._instance is None
        e2 = get_tts_engine(force_mode=EngineMode.MOCK)
        assert e1 is not e2

    def test_tts_infer_lock_thread_safety(self, tmp_path: Path):
        """Multiple concurrent threads calling synthesize must be serialized by inference lock."""
        engine = get_tts_engine(force_mode=EngineMode.MOCK)
        num_threads = 6
        errors = []

        def worker(idx: int):
            try:
                out = tmp_path / f"thread_{idx}.wav"
                dur, sha, cached = engine.synthesize_utterance(
                    text=f"Câu thoại đa luồng kiểm thử số {idx}",
                    voice="Ngọc Huyền",
                    output_path=out,
                )
                assert out.exists()
                assert dur > 0.0
            except Exception as ex:
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent TTS synthesis produced errors: {errors}"
        assert engine.utterance_count >= num_threads
        assert engine.total_inference_seconds > 0.0

    def test_tts_telemetry_metrics_tracking(self, tmp_path: Path):
        """VieNeuEngine accurately tracks init_duration_seconds, total_inference_seconds, utterance_count."""
        engine = get_tts_engine(force_mode=EngineMode.MOCK)
        assert hasattr(engine, "init_duration_seconds")
        assert hasattr(engine, "total_inference_seconds")
        assert hasattr(engine, "utterance_count")
        assert engine.utterance_count == 0

        out1 = tmp_path / "test1.wav"
        engine.synthesize_utterance(text="Kiểm tra chỉ số đo lường", voice="Ngọc Huyền", output_path=out1)
        assert engine.utterance_count == 1
        assert engine.total_inference_seconds > 0.0

        out2 = tmp_path / "test2.wav"
        engine.synthesize_utterance(text="Thêm một câu nữa", voice="Ngọc Huyền", output_path=out2)
        assert engine.utterance_count == 2


# ==============================================================================
# 2. Hardware Encoder Probing, Smoke Testing, and Fallback Tests
# ==============================================================================

class TestHardwareEncoderProbingAndFallback:
    def test_probe_available_encoders_parsing(self):
        """probe_available_encoders parses ffmpeg -encoders stdout accurately."""
        sample_stdout = """
Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 ------
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V..... h264_nvenc           NVIDIA NVENC H.264 encoder
 V..... h264_videotoolbox    VideoToolbox H.264 Encoder
 V..... libvpx-vp9           libvpx VP9
 A..... aac                  AAC (Advanced Audio Coding)
        """
        mock_res = MagicMock()
        mock_res.stdout = sample_stdout
        mock_res.returncode = 0

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch("subprocess.run", return_value=mock_res):
            encoders = probe_available_encoders()
            assert "libx264" in encoders
            assert "h264_nvenc" in encoders
            assert "h264_videotoolbox" in encoders
            assert "aac" not in encoders
            assert "libvpx-vp9" not in encoders

    def test_probe_available_encoders_missing_ffmpeg(self):
        """probe_available_encoders returns empty list if ffmpeg is not found."""
        with patch("shutil.which", return_value=None):
            assert probe_available_encoders() == []

    def test_smoke_test_encoder_success_and_failure(self):
        """smoke_test_encoder tests 0.1s null muxer command."""
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            # Test success (code 0)
            mock_success = MagicMock(returncode=0)
            with patch("subprocess.run", return_value=mock_success):
                assert smoke_test_encoder("libx264") is True

            # Test failure (code 8)
            mock_failure = MagicMock(returncode=8)
            with patch("subprocess.run", return_value=mock_failure):
                assert smoke_test_encoder("h264_nvenc") is False

            # Test timeout/exception
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5)):
                assert smoke_test_encoder("h264_nvenc") is False

    def test_resolve_encoder_auto_nvenc_priority(self):
        """When NVENC is available and passes smoke test, auto resolution picks h264_nvenc."""
        with patch("src.production.hardware.probe_available_encoders", return_value=["libx264", "h264_nvenc"]), \
             patch("src.production.hardware.smoke_test_encoder", return_value=True):
            enc, args, msg = resolve_video_encoder("auto")
            assert enc == "h264_nvenc"
            assert "NVIDIA NVENC" in msg
            assert "-c:v" in args and "h264_nvenc" in args

    def test_resolve_encoder_auto_videotoolbox_on_darwin(self):
        """When NVENC is absent but VideoToolbox passes, auto resolution picks h264_videotoolbox."""
        def mock_smoke(name: str):
            return name in ("libx264", "h264_videotoolbox")

        with patch("src.production.hardware.probe_available_encoders", return_value=["libx264", "h264_videotoolbox"]), \
             patch("src.production.hardware.smoke_test_encoder", side_effect=mock_smoke):
            enc, args, msg = resolve_video_encoder("auto")
            assert enc == "h264_videotoolbox"
            assert "VideoToolbox" in msg
            assert "-c:v" in args and "h264_videotoolbox" in args

    def test_resolve_encoder_auto_fallback_to_libx264(self):
        """When no hardware encoder is available or working, auto falls back to libx264 CPU."""
        def mock_smoke(name: str):
            return name == "libx264"

        with patch("src.production.hardware.probe_available_encoders", return_value=["libx264"]), \
             patch("src.production.hardware.smoke_test_encoder", side_effect=mock_smoke):
            enc, args, msg = resolve_video_encoder("auto")
            assert enc == "libx264"
            assert "CPU" in msg

    def test_resolve_encoder_requested_hardware_fails_graceful_fallback(self):
        """When user requests h264_nvenc but smoke test fails, gracefully falls back to libx264."""
        with patch("src.production.hardware.probe_available_encoders", return_value=["libx264", "h264_nvenc"]), \
             patch("src.production.hardware.smoke_test_encoder", return_value=False):
            enc, args, msg = resolve_video_encoder("h264_nvenc")
            assert enc == "libx264"
            assert "Fallback to libx264" in msg

    def test_resolve_encoder_alias_tuple_signature(self):
        """resolve_encoder convenience alias returns (encoder, codec_args, msg) 3-tuple."""
        with patch("src.production.hardware.probe_available_encoders", return_value=["libx264"]), \
             patch("src.production.hardware.smoke_test_encoder", return_value=True):
            res = resolve_encoder("auto")
            assert len(res) == 3
            enc, args, msg = res
            assert enc == "libx264"
            assert isinstance(args, list)
            assert isinstance(msg, str)

    def test_get_encoder_codec_args_nvenc_and_videotoolbox(self):
        """get_encoder_codec_args returns proper flags for each encoder."""
        nvenc_args = get_encoder_codec_args("h264_nvenc")
        assert "-c:v" in nvenc_args and "h264_nvenc" in nvenc_args
        assert "-preset" in nvenc_args and "p4" in nvenc_args

        vt_args = get_encoder_codec_args("h264_videotoolbox")
        assert "-c:v" in vt_args and "h264_videotoolbox" in vt_args
        assert "-prio_speed" in vt_args

        cpu_args = get_encoder_codec_args("libx264", preset="fast", crf="21")
        assert "-c:v" in cpu_args and "libx264" in cpu_args
        assert "fast" in cpu_args
        assert "21" in cpu_args


# ==============================================================================
# 3. Video Engine Render Cache Fingerprint Invalidation Tests
# ==============================================================================

class TestVideoEngineRenderCacheFingerprint:
    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# 4. Production Job Manifest Schemas & Zero Secrets Guarantee Tests
# ==============================================================================

class TestProductionJobManifestAndSchemas:
    def test_production_job_manifest_serialization(self, tmp_path: Path):
        """ProductionJobManifest serializes and deserializes accurately with zero loss."""
        job_file = tmp_path / "production_job.json"
        config = ProductionJobConfig(
            project_slug="demo_series",
            target_chapters=["1", "2", "3"],
            encoder="h264_nvenc",
            tts_engine="mock",
            quality="final",
        )
        manifest = ProductionJobManifest(
            project_slug="demo_series",
            config=config,
            chapters={
                "1": ChapterProductionProgress(chapter_number="1", tts_status=ProductionStageStatus.COMPLETED),
                "2": ChapterProductionProgress(chapter_number="2", tts_status=ProductionStageStatus.PENDING),
            },
            telemetry=ProductionJobTelemetry(
                resolved_encoder="h264_nvenc",
                tts_total_seconds=12.4,
                render_total_seconds=45.2,
            ),
        )
        manifest.save(job_file)
        assert job_file.exists()

        loaded = ProductionJobManifest.load(job_file)
        assert loaded.schema_version == "3.0.0"
        assert loaded.project_slug == "demo_series"
        assert loaded.config.encoder == "h264_nvenc"
        assert loaded.chapters["1"].tts_status == ProductionStageStatus.COMPLETED
        assert loaded.telemetry.tts_total_seconds == 12.4

    def test_production_job_zero_secrets_audit(self):
        """audit_secrets() succeeds for clean manifests and raises ValueError on API key leaks."""
        clean_manifest = ProductionJobManifest(
            project_slug="clean_manga",
            config=ProductionJobConfig(project_slug="clean_manga", target_chapters=["1"]),
        )
        assert clean_manifest.audit_secrets() is True

        # Inject fake API key into config
        leaky_manifest = ProductionJobManifest(
            project_slug="leaky_manga",
            config=ProductionJobConfig(
                project_slug="leaky_manga",
                target_chapters=["1"],
                voice_override="sk-proj-1234567890abcdef",
            ),
        )
        with pytest.raises(ValueError, match="Secret leakage audit violation"):
            leaky_manifest.audit_secrets()


# ==============================================================================
# 5. SingleSessionProductionRunner Tests (4-Stage Pipeline & Crash-Resume)
# ==============================================================================

class TestSingleSessionProductionRunner:
    def test_single_session_runner_load_or_create_fresh(self, mock_cloud_project):
        """load_or_create initializes a new manifest with target chapters."""
        p_slug = mock_cloud_project["project_slug"]
        p_dir = mock_cloud_project["project_dir"]

        runner = SingleSessionProductionRunner.load_or_create(
            project_slug=p_slug,
            config=ProductionJobConfig(project_slug=p_slug, target_chapters=["1", "2"]),
        )
        assert runner.manifest.project_slug == p_slug
        assert set(runner.manifest.chapters.keys()) == {"1", "2"}
        assert (p_dir / "production_job.json").exists()

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass


# ==============================================================================
# 6. CLI Dispatch and Flag Synchronization Tests
# ==============================================================================

class TestCliPhaseCIntegration:
    def test_cli_produce_argument_parsing(self):
        """CLI parser accepts all produce options and defaults."""
        parser = build_parser()
        args = parser.parse_args([
            "produce",
            "-p", "test_proj",
            "-c", "1-3",
            "--encoder", "h264_nvenc",
            "--tts-engine", "neural",
            "--tts-workers", "4",
            "--render-workers", "3",
            "--quality", "final",
            "--no-compile",
            "--no-export-bundle",
            "--force",
        ])
        assert args.project == "test_proj"
        assert args.chapters == "1-3"
        assert args.encoder == "h264_nvenc"
        assert args.tts_engine == "neural"
        assert args.tts_workers == 4
        assert args.render_workers == 3
        assert args.quality == "final"
        assert args.compile is False
        assert args.export_bundle is False
        assert args.force is True

    def test_cli_render_encoder_forwarding(self):
        """manga-recap render accepts and resolves --encoder option."""
        parser = build_parser()
        args = parser.parse_args([
            "render",
            "-p", "test_proj",
            "-c", "1",
            "--encoder", "libx264",
        ])
        assert args.encoder == "libx264"

    def test_cli_batch_encoder_forwarding(self):
        """manga-recap batch accepts and forwards --encoder and tts flags."""
        parser = build_parser()
        args = parser.parse_args([
            "batch",
            "-p", "test_proj",
            "-c", "1,2",
            "--encoder", "h264_videotoolbox",
            "--tts-engine", "neural",
            "--tts-workers", "2",
            "--render-workers", "2",
        ])
        assert args.encoder == "h264_videotoolbox"
        assert args.tts_engine == "neural"
        assert args.tts_workers == 2
        assert args.render_workers == 2

    # Legacy scenario consolidated; see docs/verification/v4_regression_migration.json.
    pass
