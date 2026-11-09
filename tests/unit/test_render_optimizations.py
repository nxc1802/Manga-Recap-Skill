"""
tests/unit/test_render_optimizations.py
Comprehensive unit tests for Phase 5 Render & Cloud Optimizations (A8):
1. Single-Frame Static Motion Hold Canvas Generation & Layouts (Portrait Padded, Landscape Fit, Portrait Vertical, Static Focus)
2. In-Memory Thread-Safe Canvas Cache & Invalidation
3. Single-Frame Static Motion Hold Execution & Filtercomplex Bypass in generate_panel_clip
4. Robust Hardware Encoder Selection, Serialization Mutex, and Automatic Software Fallback to libx264
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.core.schemas import CameraMotionType, FocusBox, VideoMode
from src.production.hardware import (
    get_encoder_codec_args,
    get_fallback_encoder,
    get_hardware_encoder_lock,
    is_hardware_encoder,
    resolve_video_encoder,
)
from src.render.subtitle import SubtitleCue
from src.render.video_engine import VideoEngine


def make_test_wav(path: Path, duration_sec: float = 1.0, sample_rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_sec * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = bytearray()
        for i in range(num_samples):
            v = int(6000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            raw.extend(struct.pack("<h", v))
        wf.writeframes(raw)
    return path


def make_test_image(path: Path, width: int = 800, height: int = 1200, color=(140, 90, 180)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(path, "JPEG")
    return path


class TestStaticCanvasGeneration:
    """Tests for VideoEngine.create_static_canvas across all canonical layouts."""

    def setup_method(self):
        self.engine = VideoEngine(target_width=1920, target_height=1080, fps=30)

    def test_portrait_padded_canvas_layout_and_resolution(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        canvas = self.engine.create_static_canvas(
            img_path=img_p,
            video_mode=VideoMode.PORTRAIT_PADDED,
            motion_type=CameraMotionType.STATIC,
        )
        assert canvas.size == (1920, 1080)
        assert canvas.mode == "RGB"

    def test_landscape_fit_canvas_layout(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel_wide.jpg", width=1200, height=1200)
        canvas = self.engine.create_static_canvas(
            img_path=img_p,
            video_mode=VideoMode.LANDSCAPE_FIT,
            motion_type=CameraMotionType.STATIC,
        )
        assert canvas.size == (1920, 1080)
        # Background horizontal borders outside the 1:1 image must be black
        corners = [
            canvas.getpixel((10, 540)),
            canvas.getpixel((1910, 540)),
        ]
        assert all(c == (0, 0, 0) for c in corners)

    def test_portrait_vertical_canvas_layout(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel_tall.jpg", width=600, height=1200)
        canvas = self.engine.create_static_canvas(
            img_path=img_p,
            video_mode=VideoMode.PORTRAIT_VERTICAL,
            motion_type=CameraMotionType.STATIC,
        )
        assert canvas.size == (1920, 1080)
        # Horizontal wings outside the image must be black
        assert canvas.getpixel((10, 540)) == (0, 0, 0)
        assert canvas.getpixel((1910, 540)) == (0, 0, 0)

    def test_direct_16_9_aspect_match_resize(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel_16_9.jpg", width=1280, height=720)
        canvas = self.engine.create_static_canvas(
            img_path=img_p,
            video_mode=VideoMode.AUTO,
            motion_type=CameraMotionType.STATIC,
        )
        assert canvas.size == (1920, 1080)

    def test_static_focus_and_focus_crop_canvas(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel_focus.jpg", width=1000, height=1500)
        fb = FocusBox(x_min=0.3, y_min=0.2, x_max=0.7, y_max=0.6)
        canvas = self.engine.create_static_canvas(
            img_path=img_p,
            video_mode=VideoMode.AUTO,
            focus_box=fb,
            motion_type=CameraMotionType.STATIC_FOCUS,
        )
        assert canvas.size == (1920, 1080)

    def test_in_memory_canvas_cache_reuse_and_clear(self, tmp_path):
        img_p = make_test_image(tmp_path / "panel_cached.jpg", width=800, height=1200)
        self.engine.clear_canvas_cache()
        assert len(self.engine._canvas_cache) == 0

        c1 = self.engine.create_static_canvas(img_p, video_mode=VideoMode.PORTRAIT_PADDED)
        assert len(self.engine._canvas_cache) == 1

        # Second call must hit cache
        c2 = self.engine.create_static_canvas(img_p, video_mode=VideoMode.PORTRAIT_PADDED)
        assert len(self.engine._canvas_cache) == 1
        assert c1.size == c2.size

        # Clearing cache reclaims memory
        self.engine.clear_canvas_cache()
        assert len(self.engine._canvas_cache) == 0


class TestSingleFrameStaticMotionHoldExecution:
    """Verifies that generate_panel_clip utilizes the single-frame static motion hold."""

    def setup_method(self):
        self.engine = VideoEngine(target_width=1280, target_height=720, fps=30)

    def test_static_motion_uses_loop_input_and_no_complex_filter_when_no_subs(self, tmp_path):
        panel_img = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=2.0)
        out_clip = tmp_path / "out_static.mp4"

        captured_cmds = []

        def spy_run_ffmpeg(cmd, description=""):
            captured_cmds.append(list(cmd))
            # Create dummy output file to satisfy existence check
            Path(cmd[-1]).write_bytes(b"\x00" * 4000)

        with patch("src.render.video_engine.run_ffmpeg", side_effect=spy_run_ffmpeg):
            ok = self.engine.generate_panel_clip(
                img_path=str(panel_img),
                audio_path=str(audio_wav),
                duration_ms=2000,
                output_path=str(out_clip),
                camera_motion=CameraMotionType.STATIC,
                burn_subtitles=False,
            )

        assert ok is True
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]

        # Must use -loop 1 on a single static canvas image
        assert "-loop" in cmd
        loop_idx = cmd.index("-loop")
        assert cmd[loop_idx + 1] == "1"
        assert cmd[loop_idx + 2] == "-i"
        assert "static_canvas.png" in cmd[loop_idx + 3]

        # No expensive filter_complex (boxblur) when there are no subtitles
        assert "-filter_complex" not in cmd
        assert "-map" in cmd
        map_v_idx = cmd.index("-map")
        assert cmd[map_v_idx + 1] == "0:v"

    def test_static_motion_with_subtitles_chains_overlays_on_canvas_without_boxblur(self, tmp_path):
        panel_img = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=3.0)
        out_clip = tmp_path / "out_subbed.mp4"

        cues = [
            SubtitleCue(index=1, start_seconds=0.0, end_seconds=1.5, text="Phụ đề một"),
            SubtitleCue(index=2, start_seconds=1.5, end_seconds=3.0, text="Phụ đề hai"),
        ]

        captured_cmds = []

        def spy_run_ffmpeg(cmd, description=""):
            captured_cmds.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"\x00" * 4000)

        with patch("src.render.video_engine.run_ffmpeg", side_effect=spy_run_ffmpeg):
            ok = self.engine.generate_panel_clip(
                img_path=str(panel_img),
                audio_path=str(audio_wav),
                duration_ms=3000,
                output_path=str(out_clip),
                camera_motion=CameraMotionType.STATIC,
                burn_subtitles=True,
                subtitle_cues=cues,
            )

        assert ok is True
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]

        assert "-filter_complex" in cmd
        fc_idx = cmd.index("-filter_complex")
        filter_expr = cmd[fc_idx + 1]

        # Must chain overlays on [0:v] without boxblur on every frame
        assert "boxblur" not in filter_expr
        assert "overlay=0:0:enable='between(t,0.000,1.500)'" in filter_expr
        assert "overlay=0:0:enable='between(t,1.500,3.000)'" in filter_expr
        assert "[outv]" in filter_expr

    def test_dynamic_motion_retains_camera_motion_filterchain(self, tmp_path):
        panel_img = make_test_image(tmp_path / "panel.jpg", width=1200, height=800)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=2.0)
        out_clip = tmp_path / "out_zoom.mp4"

        captured_cmds = []

        def spy_run_ffmpeg(cmd, description=""):
            captured_cmds.append(list(cmd))
            Path(cmd[-1]).write_bytes(b"\x00" * 4000)

        with patch("src.render.video_engine.run_ffmpeg", side_effect=spy_run_ffmpeg):
            ok = self.engine.generate_panel_clip(
                img_path=str(panel_img),
                audio_path=str(audio_wav),
                duration_ms=2000,
                output_path=str(out_clip),
                camera_motion=CameraMotionType.ZOOM_IN,
                burn_subtitles=False,
            )

        assert ok is True
        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "-filter_complex" in cmd
        fc_idx = cmd.index("-filter_complex")
        assert "zoompan=" in cmd[fc_idx + 1]


class TestHardwareAccelerationAndRobustFallback:
    """Verifies hardware encoder selection, mutex serialization, and fallback."""

    def test_hardware_encoder_lock_is_acquired_for_videotoolbox(self, tmp_path):
        engine = VideoEngine(target_width=1280, target_height=720, fps=30)
        panel_img = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=1.0)
        out_clip = tmp_path / "out_vt.mp4"

        lock_acquired = False
        vt_lock = get_hardware_encoder_lock()

        def spy_run_ffmpeg(cmd, description=""):
            nonlocal lock_acquired
            # In VideoToolbox path, the lock must currently be locked by the thread
            if vt_lock.locked():
                lock_acquired = True
            Path(cmd[-1]).write_bytes(b"\x00" * 4000)

        with patch("src.render.video_engine.run_ffmpeg", side_effect=spy_run_ffmpeg):
            ok = engine.generate_panel_clip(
                img_path=str(panel_img),
                audio_path=str(audio_wav),
                duration_ms=1000,
                output_path=str(out_clip),
                camera_motion=CameraMotionType.STATIC,
                encoder="h264_videotoolbox",
            )

        assert ok is True
        assert lock_acquired is True

    def test_hardware_videotoolbox_failure_falls_back_to_libx264(self, tmp_path):
        engine = VideoEngine(target_width=1280, target_height=720, fps=30)
        panel_img = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=1.0)
        out_clip = tmp_path / "out_fallback.mp4"

        attempts = []

        def spy_run_ffmpeg(cmd, description=""):
            attempts.append(list(cmd))
            # First attempt with videotoolbox simulates failure (e.g. XPC daemon crash)
            if "h264_videotoolbox" in cmd:
                raise RuntimeError("com.apple.coremedia.videoencoder XPC_ERROR_CONNECTION_INTERRUPTED")
            # Fallback attempt with libx264 succeeds
            Path(cmd[-1]).write_bytes(b"\x00" * 4000)

        with patch("src.render.video_engine.run_ffmpeg", side_effect=spy_run_ffmpeg):
            ok = engine.generate_panel_clip(
                img_path=str(panel_img),
                audio_path=str(audio_wav),
                duration_ms=1000,
                output_path=str(out_clip),
                camera_motion=CameraMotionType.STATIC,
                encoder="h264_videotoolbox",
            )

        assert ok is True
        assert len(attempts) == 2
        # First attempt was VideoToolbox
        assert "h264_videotoolbox" in attempts[0]
        # Second attempt fell back to libx264
        assert "libx264" in attempts[1]
        assert "h264_videotoolbox" not in attempts[1]

    def test_hardware_encoder_utility_functions(self):
        assert is_hardware_encoder("h264_nvenc") is True
        assert is_hardware_encoder("nvenc") is True
        assert is_hardware_encoder("h264_videotoolbox") is True
        assert is_hardware_encoder("videotoolbox") is True
        assert is_hardware_encoder("libx264") is False
        assert is_hardware_encoder("auto") is False
        assert is_hardware_encoder(None) is False

        assert get_fallback_encoder() == "libx264"
        assert get_fallback_encoder("h264_videotoolbox") == "libx264"
        assert get_fallback_encoder("h264_nvenc") == "libx264"

    def test_get_encoder_codec_args_crf_and_aliases(self):
        nvenc_args = get_encoder_codec_args("nvenc", crf="18")
        assert "-c:v" in nvenc_args and "h264_nvenc" in nvenc_args
        assert "-cq" in nvenc_args and "18" in nvenc_args

        vt_args = get_encoder_codec_args("videotoolbox")
        assert "-c:v" in vt_args and "h264_videotoolbox" in vt_args

        cpu_args = get_encoder_codec_args("libx264", preset="slow", crf="21")
        assert "-preset" in cpu_args and "slow" in cpu_args
        assert "-crf" in cpu_args and "21" in cpu_args
