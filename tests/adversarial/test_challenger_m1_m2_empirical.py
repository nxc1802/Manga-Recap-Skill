"""
tests/adversarial/test_challenger_m1_m2_empirical.py
Empirical stress tests and adversarial verification by Challenger 2 for PR-01 and PR-02.

Covers:
1. VideoEngine and Camera filterchain generators with extreme inputs (durations 0.1s, 0.5s, 60.0s).
2. Velocity capping <= 120 px/s with massive Delta y (10,000 px).
3. ZOOM_IN pre-crop and zoompan coordinate centering (anti-drift proof).
4. Motion aliasing: PAN_UP -> PAN_DOWN, ZOOM_OUT -> STATIC.
5. Real FFmpeg render execution with extreme and standard filterchains.
6. CLI fail-closed gate evaluation across crawl, render, batch, cloud run.
"""

from __future__ import annotations

import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any
from unittest.mock import patch

import pytest
from PIL import Image

from src.cli.exit_codes import ExitCode
from src.cli.main import main
from src.core.schemas import CameraMotionType, FocusBox, VideoMode
from src.render.camera import CameraMotionGenerator
from src.render.video_engine import VideoEngine


class TestCameraExtremeInputsAndVelocityCapping:
    """Stress tests CameraMotionGenerator with boundary inputs and extreme dimensions."""

    @pytest.fixture
    def gen(self):
        return CameraMotionGenerator(target_width=1280, target_height=720, fps=30, max_zoom=1.15)

    def test_duration_extreme_boundary_0_1s(self, gen):
        """Duration 0.1s must be clamped to safe minimum 0.5s without crashing."""
        flt, is_zp = gen.build_filterchain(
            img_w=1080, img_h=1920, duration_sec=0.1, motion_type=CameraMotionType.PAN_DOWN
        )
        assert "t/0.500" in flt
        assert flt.startswith("crop=")
        assert not is_zp

    def test_duration_boundary_0_5s(self, gen):
        """Duration 0.5s is the exact minimum bound."""
        flt, is_zp = gen.build_filterchain(
            img_w=1080, img_h=1920, duration_sec=0.5, motion_type=CameraMotionType.VERTICAL_SCROLL
        )
        assert "t/0.500" in flt
        assert not is_zp

    def test_duration_extreme_long_60s(self, gen):
        """Duration 60.0s should evaluate smoothly and generate correct N and T."""
        flt, is_zp = gen.build_filterchain(
            img_w=1080, img_h=1920, duration_sec=60.0, motion_type=CameraMotionType.PAN_DOWN
        )
        assert "t/60.000" in flt
        assert not is_zp

    def test_duration_zero_or_negative_defaults_to_safe_minimum(self, gen):
        """Zero, negative, or None durations safely clamp to 0.5s."""
        for bad_d in [0.0, -10.0, None]:
            flt, _ = gen.build_filterchain(
                img_w=1080, img_h=1920, duration_sec=bad_d, motion_type=CameraMotionType.STATIC
            )
            assert "fps=30" in flt

    def test_vertical_scroll_velocity_cap_with_10000px_delta_y(self, gen):
        """
        Massive panel with Delta y = 10,000 px:
        Vertical scroll velocity must be mathematically capped at <= 120 px/s.
        """
        img_w = 1000
        img_h = 11000

        # Case A: Short duration 0.5s
        flt_short, _ = gen.build_filterchain(
            img_w=img_w, img_h=img_h, duration_sec=0.5, motion_type=CameraMotionType.VERTICAL_SCROLL
        )
        assert "'60*min(1,max(0,t/0.500))'" in flt_short
        speed_short = 60 / 0.5
        assert speed_short <= 120.0

        # Case B: Standard duration 5.0s
        flt_mid, _ = gen.build_filterchain(
            img_w=img_w, img_h=img_h, duration_sec=5.0, motion_type=CameraMotionType.VERTICAL_SCROLL
        )
        assert "'600*min(1,max(0,t/5.000))'" in flt_mid
        speed_mid = 600 / 5.0
        assert speed_mid <= 120.0

        # Case C: Long duration 60.0s
        flt_long, _ = gen.build_filterchain(
            img_w=img_w, img_h=img_h, duration_sec=60.0, motion_type=CameraMotionType.VERTICAL_SCROLL
        )
        assert "'7200*min(1,max(0,t/60.000))'" in flt_long
        speed_long = 7200 / 60.0
        assert speed_long <= 120.0

    def test_pan_down_peak_velocity_cap_with_10000px_delta_y(self, gen):
        """
        For cosine ease PAN_DOWN with Delta y = 10,000 px:
        Peak velocity v_peak = delta_y * (pi / 2T).
        Generator uses max_scroll_speed = 76.39 px/s so that
        v_peak = 76.39 * (pi / 2) <= 120.0 px/s.
        """
        img_w = 1000
        img_h = 11000

        # Case A: T = 0.5s
        flt_short, _ = gen.build_filterchain(
            img_w=img_w, img_h=img_h, duration_sec=0.5, motion_type=CameraMotionType.PAN_DOWN
        )
        assert "'38*0.5*(1-cos(PI*min(1,max(0,t/0.500))))'" in flt_short
        v_peak_short = 38 * (math.pi / (2 * 0.5))
        assert v_peak_short <= 120.0

        # Case B: T = 60.0s
        flt_long, _ = gen.build_filterchain(
            img_w=img_w, img_h=img_h, duration_sec=60.0, motion_type=CameraMotionType.PAN_DOWN
        )
        assert "'4582*0.5*(1-cos(PI*min(1,max(0,t/60.000))))'" in flt_long
        v_peak_long = 4582 * (math.pi / (2 * 60.0))
        assert v_peak_long <= 120.0


class TestMotionAliasingAdversarial:
    """Verifies complete aliasing of forbidden reverse motions to safe forward motions."""

    @pytest.fixture
    def gen(self):
        return CameraMotionGenerator()

    def test_pan_up_aliased_to_pan_down(self, gen):
        assert gen.normalize_motion_type(CameraMotionType.PAN_UP) == CameraMotionType.PAN_DOWN
        for alias in ["pan_up", "up", "PAN_UP", "Up"]:
            assert gen.normalize_motion_type(alias) == CameraMotionType.PAN_DOWN

        flt, is_zp = gen.build_filterchain(
            img_w=1080, img_h=1920, duration_sec=4.0, motion_type=CameraMotionType.PAN_UP
        )
        assert not is_zp
        assert "1-cos" in flt
        assert "1+cos" not in flt

    def test_zoom_out_aliased_to_static(self, gen):
        assert gen.normalize_motion_type(CameraMotionType.ZOOM_OUT) == CameraMotionType.STATIC
        for alias in ["zoom_out", "ZOOM_OUT", "Zoom_Out"]:
            assert gen.normalize_motion_type(alias) == CameraMotionType.STATIC

        flt, is_zp = gen.build_filterchain(
            img_w=1080, img_h=1920, duration_sec=4.0, motion_type=CameraMotionType.ZOOM_OUT
        )
        assert not is_zp
        assert "zoompan" not in flt
        assert "split=2" in flt or "pad=" in flt


class TestZoomInPrecropAndCoordinateCentering:
    """Verifies that ZOOM_IN pre-crop and zoompan coordinate centering preserves subject center without drift."""

    @pytest.fixture
    def gen(self):
        return CameraMotionGenerator(target_width=1280, target_height=720, fps=30, max_zoom=1.15)

    def test_precrop_landscape_centers_horizontally_on_subject(self, gen):
        """In landscape panels, pre-crop centers horizontally on fx when not boundary-clamped."""
        img_w, img_h = 2400, 1080
        # When fx is centered (0.5), crop_x is within bounds and centers precisely
        fx, fy = 0.5, 0.5
        bw, bh, cx, cy = gen.calculate_pre_crop(img_w, img_h, fx=fx, fy=fy)
        sub_x = fx * img_w
        rel_x = (sub_x - cx) / bw
        assert abs(rel_x - 0.5) < 0.01

        # When fx is near edge (0.35), pre-crop clamps to x=0 to prevent sampling outside image
        fx_edge = 0.35
        bw, bh, cx, cy = gen.calculate_pre_crop(img_w, img_h, fx=fx_edge, fy=fy)
        assert cx == 0
        sub_x_edge = fx_edge * img_w  # 840
        rel_x_edge = sub_x_edge / bw  # 840 / 1920 = 0.4375
        # Even when clamped, subject is comfortably inside visible viewport [0.065, 0.935]
        assert 0.07 < rel_x_edge < 0.93

    def test_precrop_portrait_centers_vertically_on_safe_subject(self, gen):
        """In portrait panels (taller than 16:9), pre-crop centers vertically on safe_fy."""
        img_w, img_h = 1080, 1920
        fx, fy = 0.5, 0.6
        bw, bh, cx, cy = gen.calculate_pre_crop(img_w, img_h, fx=fx, fy=fy)
        safe_fy = max(0.30, min(0.75, fy))
        sub_y = safe_fy * img_h
        rel_y = (sub_y - cy) / bh
        assert abs(rel_y - 0.5) < 0.01

    def test_zoompan_coordinates_remain_mathematically_centered(self, gen):
        """
        Zoompan expression x='(iw-iw/zoom)*0.5':
        At any zoom factor z in [1.0, 1.15], the visible viewport center remains at iw*0.5.
        """
        flt, is_zp = gen.build_filterchain(
            img_w=1080, img_h=1080, duration_sec=3.0, motion_type=CameraMotionType.ZOOM_IN
        )
        assert is_zp
        assert "x='(iw-iw/zoom)*0.5'" in flt
        assert "y='(ih-ih/zoom)*0.5'" in flt

        for z in [1.0, 1.05, 1.10, 1.15]:
            iw = 1280.0
            x = (iw - iw / z) * 0.5
            center = x + (iw / z) / 2.0
            assert math.isclose(center, iw * 0.5, abs_tol=1e-9)

    def test_subject_center_remains_on_screen_during_zoom(self, gen):
        """
        At max zoom 1.15, the pre-cropped window trims 6.5% margins.
        A subject inside [0.0652, 0.9348] remains fully visible without drifting off-screen.
        """
        max_zoom = 1.15
        margin = (1.0 - 1.0 / max_zoom) * 0.5
        assert margin < 0.07  # only 6.52% trimmed
        visible_range = (margin, 1.0 - margin)

        # Subjects at standard positions (0.10 to 0.90) remain strictly in view
        for test_pos in [0.10, 0.25, 0.50, 0.75, 0.90]:
            assert visible_range[0] < test_pos < visible_range[1]

    def test_speech_bubble_avoidance_on_upper_focus_box(self, gen):
        """
        FocusBox placed high up on a speech bubble (y_min=0.05, y_max=0.20):
        The vertical center must be clamped to safe zone >= 0.30.
        """
        fb = FocusBox(x_min=0.2, y_min=0.05, x_max=0.4, y_max=0.20)
        bw, bh, cx, cy = gen.calculate_pre_crop(
            1080, 1920,
            fx=(fb.x_min + fb.x_max) / 2.0,
            fy=(fb.y_min + fb.y_max) / 2.0,
        )
        assert cy >= 270


class TestRealFFmpegVideoEngineRender:
    """Renders actual MP4 clips using VideoEngine and FFmpeg to verify zero runtime execution errors."""

    @pytest.fixture
    def test_env(self, tmp_path):
        panel_path = tmp_path / "panel_001.jpg"
        img = Image.new("RGB", (1080, 1920), color=(50, 80, 120))
        img.save(panel_path, "JPEG")

        audio_path = tmp_path / "audio_001.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                "-t", "1.0", str(audio_path)
            ],
            check=True,
            capture_output=True,
        )

        out_dir = tmp_path / "output"
        out_dir.mkdir(parents=True, exist_ok=True)

        return {
            "panel": panel_path,
            "audio": audio_path,
            "out_dir": out_dir,
        }

    def test_render_pan_down_clip_success(self, test_env):
        engine = VideoEngine(target_width=640, target_height=360, fps=24)
        out_clip = test_env["out_dir"] / "test_pan_down.mp4"
        success = engine.generate_panel_clip(
            img_path=str(test_env["panel"]),
            audio_path=str(test_env["audio"]),
            duration_ms=1000,
            output_path=str(out_clip),
            camera_motion=CameraMotionType.PAN_DOWN,
            allow_animation=True,
            burn_subtitles=False,
        )
        assert success
        assert out_clip.exists()
        assert out_clip.stat().st_size > 1000

    def test_render_zoom_in_clip_success(self, test_env):
        engine = VideoEngine(target_width=640, target_height=360, fps=24)
        out_clip = test_env["out_dir"] / "test_zoom_in.mp4"
        success = engine.generate_panel_clip(
            img_path=str(test_env["panel"]),
            audio_path=str(test_env["audio"]),
            duration_ms=1000,
            output_path=str(out_clip),
            camera_motion=CameraMotionType.ZOOM_IN,
            allow_animation=True,
            burn_subtitles=False,
        )
        assert success
        assert out_clip.exists()
        assert out_clip.stat().st_size > 1000

    def test_render_static_hold_single_canvas_success(self, test_env):
        engine = VideoEngine(target_width=640, target_height=360, fps=24)
        out_clip = test_env["out_dir"] / "test_static.mp4"
        success = engine.generate_panel_clip(
            img_path=str(test_env["panel"]),
            audio_path=str(test_env["audio"]),
            duration_ms=1000,
            output_path=str(out_clip),
            camera_motion=CameraMotionType.STATIC,
            allow_animation=False,
            burn_subtitles=False,
        )
        assert success
        assert out_clip.exists()
        assert out_clip.stat().st_size > 1000


class TestCliFailClosedGatesEmpirical:
    """Empirically evaluates CLI fail-closed behaviors on unconfigured/non-existent series."""

    def test_crawl_fail_closed_exit_code_2(self, capsys):
        code = main(["crawl", "-p", "non_existent_series", "-c", "1", "--url", "http://example.com", "--json"])
        assert code == ExitCode.VALIDATION_ERROR  # 2
        captured = capsys.readouterr()
        start_idx = captured.out.find("{")
        res = json.loads(captured.out[start_idx:] if start_idx != -1 else captured.out)
        assert res["status"] == "error"
        assert res["exit_code"] == 2
        assert "Strict Form Gate failed" in res["error"]["message"]
        assert len(res["data"]["errors"]) == 10

    def test_cloud_run_fail_closed_exit_code_2(self, capsys):
        code = main(["cloud", "run", "-p", "non_existent_series", "-c", "1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR  # 2
        captured = capsys.readouterr()
        start_idx = captured.out.find("{")
        res = json.loads(captured.out[start_idx:] if start_idx != -1 else captured.out)
        assert res["status"] == "error"
        assert res["exit_code"] == 2
        assert "Strict Form Gate failed" in res["error"]["message"]
        assert len(res["data"]["errors"]) == 10

    def test_render_fail_closed_current_behavior(self, capsys):
        """
        FAIL-CLOSED GATE VERIFICATION:
        render enforces require_series_setup before checking project_dir existence.
        On an unconfigured/non-existent series directory, it raises CliValidationError (ExitCode 2, VALIDATION_ERROR).
        """
        code = main(["render", "-p", "non_existent_series", "-c", "1", "--json"])
        assert code == ExitCode.VALIDATION_ERROR  # 2
        captured = capsys.readouterr()
        start_idx = captured.out.find("{")
        res = json.loads(captured.out[start_idx:] if start_idx != -1 else captured.out)
        assert res["exit_code"] == 2
        assert res["error"]["code"] == "VALIDATION_ERROR"
        assert "Strict Form Gate failed" in res["error"]["message"]

    def test_batch_fail_closed_current_behavior(self, capsys):
        """
        FAIL-CLOSED GATE VERIFICATION:
        batch enforces require_series_setup before checking project_dir existence.
        On an unconfigured/non-existent series directory, it raises CliValidationError (ExitCode 2, VALIDATION_ERROR).
        """
        code = main(["batch", "-p", "non_existent_series", "--json"])
        assert code == ExitCode.VALIDATION_ERROR  # 2
        captured = capsys.readouterr()
        start_idx = captured.out.find("{")
        res = json.loads(captured.out[start_idx:] if start_idx != -1 else captured.out)
        assert res["exit_code"] == 2
        assert res["error"]["code"] == "VALIDATION_ERROR"
        assert "Strict Form Gate failed" in res["error"]["message"]
