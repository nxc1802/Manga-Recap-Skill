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

    init_project_layout(project_slug, title="Test Cloud Project")
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
    def test_render_fingerprint_includes_encoder(self, mock_cloud_project):
        """Render cache metadata contains resolved encoder name."""
        p_dir = mock_cloud_project["project_dir"]
        engine = VideoEngine()

        def fake_generate_clip(**kwargs):
            out = Path(kwargs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * 2000)
            return True

        def fake_concat(clips, out):
            out_p = Path(out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(b"\x00" * 5000)
            return True

        def fake_run_ffmpeg(cmd, description=""):
            if cmd:
                out_target = Path(cmd[-1])
                out_target.parent.mkdir(parents=True, exist_ok=True)
                out_target.write_bytes(b"\x00" * 5000)

        # Mock render_chapter dependencies
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(engine, "generate_panel_clip", side_effect=fake_generate_clip), \
             patch.object(engine, "concatenate_clips", side_effect=fake_concat), \
             patch("src.render.video_engine.run_ffmpeg", side_effect=fake_run_ffmpeg), \
             patch.object(engine.validator, "validate_video", return_value={"valid": True, "duration_seconds": 5.0}):

            # Render with libx264
            res = engine.render_chapter(
                project_dir=p_dir,
                chapter_num=1,
                quality="preview",
                suffix="preview",
                encoder="libx264",
                allow_placeholders=True,
                keep_temp=True,
            )
            assert res["encoder"] == "libx264"

            # Check saved clip metadata json
            clips_dir = p_dir / "chapters" / "1" / "video" / "clips"
            meta_files = list(clips_dir.glob("*.meta.json"))
            assert len(meta_files) > 0
            meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
            assert "encoder" in meta
            assert meta["encoder"] == "libx264"

    def test_render_cache_invalidated_when_encoder_changes(self, mock_cloud_project):
        """Switching from libx264 to h264_nvenc invalidates cached clips and re-renders."""
        p_dir = mock_cloud_project["project_dir"]
        engine = VideoEngine()

        def fake_generate_clip(**kwargs):
            out = Path(kwargs["output_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"\x00" * 2000)
            return True

        def fake_concat(clips, out):
            out_p = Path(out)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(b"\x00" * 5000)
            return True

        def fake_run_ffmpeg(cmd, description=""):
            if cmd:
                out_target = Path(cmd[-1])
                out_target.parent.mkdir(parents=True, exist_ok=True)
                out_target.write_bytes(b"\x00" * 5000)

        with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             patch.object(engine, "generate_panel_clip", side_effect=fake_generate_clip), \
             patch.object(engine, "concatenate_clips", side_effect=fake_concat), \
             patch("src.render.video_engine.run_ffmpeg", side_effect=fake_run_ffmpeg), \
             patch.object(engine.validator, "validate_video", return_value={"valid": True, "duration_seconds": 5.0}):

            # 1. First run with libx264
            res1 = engine.render_chapter(
                project_dir=p_dir,
                chapter_num=1,
                quality="preview",
                suffix="preview",
                encoder="libx264",
                allow_placeholders=True,
                keep_temp=True,
            )
            assert res1["rendered_clips_count"] == 2
            assert res1["cached_clips_count"] == 0

            # 2. Second run with SAME encoder (libx264): must hit cache
            # Simulate real clip file presence with non-zero size
            clips_dir = p_dir / "chapters" / "1" / "video" / "clips"
            for c in clips_dir.glob("*.mp4"):
                c.write_bytes(b"\x00" * 2000)

            res2 = engine.render_chapter(
                project_dir=p_dir,
                chapter_num=1,
                quality="preview",
                suffix="preview",
                encoder="libx264",
                allow_placeholders=True,
                keep_temp=True,
            )
            assert res2["cached_clips_count"] == 2
            assert res2["rendered_clips_count"] == 0

            # 3. Third run with DIFFERENT encoder (h264_nvenc): cache must be invalidated!
            res3 = engine.render_chapter(
                project_dir=p_dir,
                chapter_num=1,
                quality="preview",
                suffix="preview",
                encoder="h264_nvenc",
                allow_placeholders=True,
                keep_temp=True,
            )
            assert res3["cached_clips_count"] == 0
            assert res3["rendered_clips_count"] == 2
            assert res3["encoder"] == "h264_nvenc"


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

    def test_single_session_runner_full_4_stages(self, mock_cloud_project):
        """SingleSessionProductionRunner successfully executes all 4 stages end-to-end."""
        p_slug = mock_cloud_project["project_slug"]
        p_dir = mock_cloud_project["project_dir"]

        config = ProductionJobConfig(
            project_slug=p_slug,
            target_chapters=["1", "2"],
            encoder="auto",
            tts_engine="mock",
            quality="preview",
            compile=True,
            export_bundle=True,
        )

        runner = SingleSessionProductionRunner.load_or_create(
            project_slug=p_slug,
            config=config,
            force=True,
        )

        # Mock heavy dependencies (ffmpeg rendering and compilation)
        dummy_mp4_1 = p_dir / "chapters" / "1" / "video" / "chapter_1_preview.mp4"
        dummy_mp4_2 = p_dir / "chapters" / "2" / "video" / "chapter_2_preview.mp4"
        dummy_srt_1 = p_dir / "chapters" / "1" / "subtitles" / "chapter_1.srt"
        dummy_srt_2 = p_dir / "chapters" / "2" / "subtitles" / "chapter_2.srt"

        dummy_mp4_1.parent.mkdir(parents=True, exist_ok=True)
        dummy_mp4_2.parent.mkdir(parents=True, exist_ok=True)
        dummy_srt_1.parent.mkdir(parents=True, exist_ok=True)
        dummy_srt_2.parent.mkdir(parents=True, exist_ok=True)

        dummy_mp4_1.write_bytes(b"\x00" * 5000)
        dummy_mp4_2.write_bytes(b"\x00" * 5000)
        dummy_srt_1.write_text("1\n00:00:00,000 --> 00:00:02,000\nSub 1\n", encoding="utf-8")
        dummy_srt_2.write_text("1\n00:00:00,000 --> 00:00:02,000\nSub 2\n", encoding="utf-8")

        mock_render_result_1 = {
            "video_path": str(dummy_mp4_1),
            "subtitle_path": str(dummy_srt_1),
            "export_video_path": str(dummy_mp4_1),
            "export_subtitle_path": str(dummy_srt_1),
            "cached_clips_count": 0,
            "rendered_clips_count": 2,
        }
        mock_render_result_2 = {
            "video_path": str(dummy_mp4_2),
            "subtitle_path": str(dummy_srt_2),
            "export_video_path": str(dummy_mp4_2),
            "export_subtitle_path": str(dummy_srt_2),
            "cached_clips_count": 0,
            "rendered_clips_count": 2,
        }

        compiled_mp4 = p_dir / "final_recap.mp4"
        compiled_mp4.write_bytes(b"\x00" * 10000)
        feature_tl = p_dir / "feature_timeline.json"
        feature_tl.write_text("{}", encoding="utf-8")

        mock_compile_result = {
            "compiled_video": str(compiled_mp4),
            "compiled_audio": None,
            "feature_timeline": str(feature_tl),
        }

        with patch("src.production.runner.handle_render", side_effect=[mock_render_result_1, mock_render_result_2]), \
             patch("src.production.runner.handle_compile", return_value=mock_compile_result):

            final_manifest = runner.run()

            # Verify Stage 1: TTS
            assert final_manifest.chapters["1"].tts_status == ProductionStageStatus.COMPLETED
            assert final_manifest.chapters["2"].tts_status == ProductionStageStatus.COMPLETED
            assert final_manifest.telemetry.tts_total_seconds >= 0.0

            # Verify Stage 2: Render
            assert final_manifest.chapters["1"].render_status == ProductionStageStatus.COMPLETED
            assert final_manifest.chapters["2"].render_status == ProductionStageStatus.COMPLETED
            assert final_manifest.telemetry.render_total_seconds >= 0.0

            # Verify Stage 3: Compile
            assert final_manifest.compiled_video_path == str(compiled_mp4)

            # Verify Stage 4: Export Bundle
            assert final_manifest.export_bundle_dir is not None
            assert len(final_manifest.artifacts) > 0

            # Verify Telemetry & Final Status
            assert final_manifest.status == ProductionStageStatus.COMPLETED
            assert final_manifest.current_stage == "completed"
            assert final_manifest.telemetry.total_wall_time_seconds > 0.0

    def test_single_session_runner_crash_resume_skips_completed(self, mock_cloud_project):
        """Runner skips stages and chapters that were already marked COMPLETED in production_job.json."""
        p_slug = mock_cloud_project["project_slug"]
        p_dir = mock_cloud_project["project_dir"]

        # Pre-seed manifest where chapter 1 TTS and Render are already COMPLETED
        config = ProductionJobConfig(
            project_slug=p_slug,
            target_chapters=["1", "2"],
            encoder="auto",
            tts_engine="mock",
            quality="preview",
            compile=False,
            export_bundle=False,
        )
        job_file = p_dir / "production_job.json"
        manifest = ProductionJobManifest(
            project_slug=p_slug,
            config=config,
            chapters={
                "1": ChapterProductionProgress(
                    chapter_number="1",
                    tts_status=ProductionStageStatus.COMPLETED,
                    render_status=ProductionStageStatus.COMPLETED,
                    video_path=str(p_dir / "ch1.mp4"),
                ),
                "2": ChapterProductionProgress(
                    chapter_number="2",
                    tts_status=ProductionStageStatus.PENDING,
                    render_status=ProductionStageStatus.PENDING,
                ),
            },
        )
        manifest.save(job_file)

        runner = SingleSessionProductionRunner.load_or_create(
            project_slug=p_slug,
            job_file_path=job_file,
            force=False,
        )

        mock_tts = MagicMock(return_value={"segments_synthesized": 2, "segments_cached": 0})
        mock_render = MagicMock(return_value={
            "video_path": str(p_dir / "ch2.mp4"),
            "subtitle_path": None,
            "cached_clips_count": 0,
            "rendered_clips_count": 2,
        })

        with patch("src.production.runner.handle_tts", mock_tts), \
             patch("src.production.runner.handle_render", mock_render):
            runner.run()

            # Chapter 1 should NOT have been passed to handle_tts or handle_render
            tts_called_chapters = [call[0][0].chapter for call in mock_tts.call_args_list]
            render_called_chapters = [call[0][0].chapter for call in mock_render.call_args_list]

            assert "1" not in tts_called_chapters, "Completed chapter 1 TTS should have been skipped"
            assert "2" in tts_called_chapters, "Pending chapter 2 TTS should have run"
            assert "1" not in render_called_chapters, "Completed chapter 1 render should have been skipped"
            assert "2" in render_called_chapters, "Pending chapter 2 render should have run"

    def test_single_session_runner_failure_isolation(self, mock_cloud_project):
        """Failure in chapter 2 does not crash chapter 1 or abort whole cloud pipeline."""
        p_slug = mock_cloud_project["project_slug"]
        config = ProductionJobConfig(
            project_slug=p_slug,
            target_chapters=["1", "2"],
            encoder="auto",
            tts_engine="mock",
            quality="preview",
            compile=True,
            export_bundle=False,
        )
        runner = SingleSessionProductionRunner.load_or_create(
            project_slug=p_slug,
            config=config,
            force=True,
        )

        def mock_render_fail_on_ch2(args):
            if str(args.chapter) == "2":
                raise RuntimeError("Simulated GPU out of memory error on chapter 2")
            return {
                "video_path": "/dummy/ch1.mp4",
                "subtitle_path": None,
                "cached_clips_count": 0,
                "rendered_clips_count": 2,
            }

        with patch("src.production.runner.handle_render", side_effect=mock_render_fail_on_ch2), \
             patch("src.production.runner.handle_compile") as mock_compile:
            mock_compile.return_value = {
                "compiled_video": "/dummy/compiled.mp4",
                "compiled_audio": None,
                "feature_timeline": "/dummy/timeline.json",
            }
            res_manifest = runner.run()

            assert res_manifest.chapters["1"].render_status == ProductionStageStatus.COMPLETED
            assert res_manifest.chapters["2"].render_status == ProductionStageStatus.FAILED
            assert "out of memory" in res_manifest.chapters["2"].error
            # Compilation should proceed with chapter 1
            assert mock_compile.called


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
            "--tts-engine", "mock",
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
        assert args.tts_engine == "mock"
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
            "--tts-engine", "mock",
            "--tts-workers", "2",
            "--render-workers", "2",
        ])
        assert args.encoder == "h264_videotoolbox"
        assert args.tts_engine == "mock"
        assert args.tts_workers == 2
        assert args.render_workers == 2

    def test_handle_produce_subcommand_dispatch(self, mock_cloud_project):
        """execute_subcommand('produce', ...) runs runner and returns envelope dict."""
        p_slug = mock_cloud_project["project_slug"]
        args = argparse.Namespace(
            project=p_slug,
            chapters="1",
            encoder="auto",
            tts_engine="mock",
            tts_workers=2,
            render_workers=2,
            quality="preview",
            video_mode="auto",
            burn_subtitles=True,
            animation=True,
            bgm=None,
            compile=False,
            export_bundle=False,
            cleanup=False,
            force=True,
            voice=None,
            speed=None,
            subtitle_lines=1,
            subtitle_font_size=26,
            subtitle_chunking="sentence-aware",
            job_file=None,
        )

        with patch("src.production.runner.handle_render") as mock_ren:
            mock_ren.return_value = {
                "video_path": "/dummy/v.mp4",
                "subtitle_path": None,
                "cached_clips_count": 0,
                "rendered_clips_count": 2,
            }
            res = handle_produce(args)
            assert res["status"] == "completed"
            assert res["project_slug"] == p_slug
            assert "target_chapters" in res
            assert "telemetry" in res
            assert res["telemetry"]["resolved_encoder"] in SUPPORTED_ENCODERS
