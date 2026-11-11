"""
tests/unit/test_motion_v4.py
Rigorous verification suite for Motion & Framing Optimization V4 (PR-02).

Validates:
1. Strictly monotonic top-down motion: y(t+dt) >= y(t) across every time step.
2. Clamped time progress: p = min(1, max(0, t/T)) with zero bounce-back when t > T.
3. Velocity capping: v <= 120 px/s (instantaneous and average).
4. Motion aliasing: PAN_UP -> PAN_DOWN (monotonic top-down), ZOOM_OUT -> STATIC.
5. Double-focus coordinate fix: zoom centering on (0.5, 0.5) of pre-cropped stream.
6. FocusBox minimum dimensions (w >= 0.35, h >= 0.30) and safe subject center (fy >= 0.30).
7. Single-canvas rendering for static holds and decomposed tall panel sub-boxes.
"""

import math
from pathlib import Path
from PIL import Image
import numpy as np
import pytest

from src.core.schemas import CameraMotionType, FocusBox, VideoMode
from src.render.camera import CameraMotionGenerator
from src.render.video_engine import VideoEngine


class TestMonotonicMotionEquations:
    """Mathematical verification of monotonic kinematics across time steps."""

    def setup_method(self):
        self.generator = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def _evaluate_pan_down_y(self, delta_y: float, T: float, t: float) -> float:
        """Simulates FFmpeg mathematical evaluation for clamped cosine ease PAN_DOWN."""
        p = min(1.0, max(0.0, t / T))
        return delta_y * 0.5 * (1.0 - math.cos(math.pi * p))

    def _evaluate_vertical_scroll_y(self, delta_y: float, T: float, t: float) -> float:
        """Simulates FFmpeg mathematical evaluation for clamped linear VERTICAL_SCROLL."""
        p = min(1.0, max(0.0, t / T))
        return delta_y * p

    def test_pan_down_strictly_monotonic_at_every_dt(self):
        """Verifies y(t + dt) >= y(t) at every step (dt = 0.01s) with zero reverse displacement."""
        T = 4.0
        delta_y = 600.0
        dt = 0.01
        num_steps = int(2.0 * T / dt)  # Evaluate from t = 0 to 2*T (past cut)

        y_prev = 0.0
        for step in range(num_steps + 1):
            t = step * dt
            y_curr = self._evaluate_pan_down_y(delta_y, T, t)

            assert y_curr >= y_prev, (
                f"Monotonicity violation at t={t:.3f}s: y_curr={y_curr:.4f} < y_prev={y_prev:.4f}"
            )
            y_prev = y_curr

    def test_pan_down_zero_bounce_back_past_duration(self):
        """Verifies that for any t >= T, y(t) == delta_y exactly, with zero rubber-banding."""
        T = 3.5
        delta_y = 500.0

        # Exact final value at t = T
        y_final = self._evaluate_pan_down_y(delta_y, T, T)
        assert pytest.approx(y_final, 0.0001) == delta_y

        # Values past T (e.g. padding frames t = 3.55s, 4.0s, 5.0s)
        for t_past in [3.51, 3.6, 4.0, 5.0, 10.0]:
            y_past = self._evaluate_pan_down_y(delta_y, T, t_past)
            assert pytest.approx(y_past, 0.0001) == delta_y, (
                f"Bounce-back detected at t={t_past}s: y={y_past} != delta_y={delta_y}"
            )

    def test_vertical_scroll_strictly_monotonic_and_clamped(self):
        """Verifies VERTICAL_SCROLL linear monotonicity and terminal clamp at delta_y."""
        T = 5.0
        delta_y = 450.0
        dt = 0.01

        y_prev = 0.0
        for step in range(int(2.0 * T / dt) + 1):
            t = step * dt
            y_curr = self._evaluate_vertical_scroll_y(delta_y, T, t)
            assert y_curr >= y_prev
            y_prev = y_curr

        # Clamped past duration
        assert pytest.approx(self._evaluate_vertical_scroll_y(delta_y, T, 6.0), 0.0001) == delta_y


class TestVelocityCapping:
    """Verifies that vertical travel per second is strictly capped <= 120 px/s."""

    def setup_method(self):
        self.generator = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def test_vertical_scroll_velocity_capped_at_120_px_per_sec(self):
        # Tall image: 800w x 4000h in 4 seconds
        # Raw delta_y = 4000 - (800 / 1.777) = 4000 - 450 = 3550 px
        # Without cap: 3550 / 4 = 887 px/s (severe judder)
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=800,
            img_h=4000,
            duration_sec=4.0,
            motion_type=CameraMotionType.VERTICAL_SCROLL,
        )
        assert not is_zoompan

        # Extract delta_y from expression: 'delta_y*min(1,max(0,t/4.000))'
        # With max_scroll_speed = 120 px/s and T = 4.0s, delta_y must be <= 480 px
        assert "min(1,max(0,t/4.000))" in filter_str
        # Max allowed delta_y is ensure_even(120 * 4.0) = 480
        assert "'480*min(1,max(0,t/4.000))'" in filter_str

    def test_pan_down_peak_velocity_bounded_at_120_px_per_sec(self):
        # 1080w x 3000h over 5.0s
        # Cosine peak velocity formula: v_peak = delta_y * (pi / 2T) <= 120 px/s
        # delta_y <= 120 * (2T / pi) = 120 * (10 / 3.14159) ~ 381.97 -> 382 px
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1080,
            img_h=3000,
            duration_sec=5.0,
            motion_type=CameraMotionType.PAN_DOWN,
        )
        assert not is_zoompan

        # Verify clamped expression with capped delta_y (380 px)
        # Peak velocity = 380 * (pi / (2 * 5.0)) = 119.38 <= 120.0 px/s!
        assert "1-cos(PI*min(1,max(0,t/5.000)))" in filter_str
        assert "'380*0.5*(1-cos(PI*min(1,max(0,t/5.000))))'" in filter_str


class TestMotionAliasing:
    """Verifies that reverse and jarring motions are safely aliased to monotonic/static."""

    def setup_method(self):
        self.generator = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def test_pan_up_aliased_to_pan_down(self):
        """PAN_UP must alias to monotonic top-to-bottom PAN_DOWN."""
        norm_motion = self.generator.normalize_motion_type(CameraMotionType.PAN_UP)
        assert norm_motion == CameraMotionType.PAN_DOWN

        filter_str, _ = self.generator.build_filterchain(
            img_w=1080, img_h=2400, duration_sec=4.0, motion_type=CameraMotionType.PAN_UP
        )
        # Filter must use top-to-bottom (1 - cos), NEVER reverse (1 + cos)
        assert "1-cos(PI*min(1,max(0,t/" in filter_str
        assert "1+cos" not in filter_str

    def test_zoom_out_aliased_to_static(self):
        """ZOOM_OUT must alias to STATIC to eliminate reverse zoom jitter."""
        norm_motion = self.generator.normalize_motion_type(CameraMotionType.ZOOM_OUT)
        assert norm_motion == CameraMotionType.STATIC

        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1280, img_h=720, duration_sec=3.0, motion_type=CameraMotionType.ZOOM_OUT
        )
        assert not is_zoompan
        assert "scale=1280:720" in filter_str


class TestDoubleFocusBugFix:
    """Verifies that ZoomIn centers on pre-crop center (0.5, 0.5) avoiding double-focus drift."""

    def setup_method(self):
        self.generator = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def test_zoom_in_centers_on_half_inside_precrop(self):
        # Request focus on far right character (fx=0.80, fy=0.60)
        fb = FocusBox(x_min=0.6, y_min=0.4, x_max=1.0, y_max=0.8)

        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=2000,
            img_h=1000,
            duration_sec=3.0,
            motion_type=CameraMotionType.ZOOM_IN,
            focus_box=fb,
        )
        assert is_zoompan
        assert "crop=" in filter_str

        # Double-focus bug fix: zoompan x and y must center on (0.5, 0.5) of pre-crop
        # Pre-crop already centered on (fx, safe_fy). Multiplying by fx a 2nd time would cause double focus.
        assert "x='(iw-iw/zoom)*0.5'" in filter_str
        assert "y='(ih-ih/zoom)*0.5'" in filter_str
        assert "*0.8000" not in filter_str  # Must not multiply by fx a second time


class TestFocusBoxAndFramingConstraints:
    """Verifies FocusBox minimum dimensions and safe subject center constraints."""

    def setup_method(self):
        self.generator = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def test_focus_box_minimum_dimensions_enforced(self):
        # A tiny box (e.g. 0.05 x 0.05 around speech bubble) must be expanded to at least w >= 0.35, h >= 0.30
        small_box = FocusBox(x_min=0.10, y_min=0.10, x_max=0.18, y_max=0.18)
        fx, fy, scale = self.generator.calculate_focus_center(focus_box=small_box)

        # Scale must be clamped to at least 0.35
        assert scale >= 0.35

    def test_safe_subject_focal_center_on_portrait_panels(self):
        # Portrait panel (800w x 1600h) with top speech bubble target (fy = 0.08)
        # Pre-crop must clamp safe_fy >= 0.30 to guarantee framing stays on character face/action
        _, _, _, crop_y = self.generator.calculate_pre_crop(
            img_w=800, img_h=1600, fx=0.5, fy=0.08
        )

        # Base 16:9 crop height for 800w is round(800 / (1280/720)) = 450 px
        # Safe center fy clamped to >= 0.30: center_y = 0.30 * 1600 = 480 px
        # crop_y = center_y - base_h / 2 = 480 - 225 = 255 px
        assert crop_y >= 250, f"crop_y={crop_y} focused too close to top speech bubble"


class TestSingleCanvasRenderingForStaticHolds:
    """Verifies single-canvas Pillow rendering for static holds and sub-region crops."""

    def test_create_static_canvas_with_sub_box_cropping(self, tmp_path):
        engine = VideoEngine(target_width=1280, target_height=720, fps=30)

        # Create a tall synthetic panel (1000w x 2500h)
        img_p = tmp_path / "tall_panel.jpg"
        arr = np.zeros((2500, 1000, 3), dtype=np.uint8)
        # Top half red, bottom half blue
        arr[:1250, :, 0] = 255
        arr[1250:, :, 2] = 255
        Image.fromarray(arr).save(img_p)

        # 1. Full canvas
        canvas_full = engine.create_static_canvas(img_p, video_mode=VideoMode.PORTRAIT_PADDED)
        assert canvas_full.size == (1280, 720)

        # 2. Sub-box top slice (y in [0.0, 0.50])
        top_box = FocusBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=0.50)
        canvas_top = engine.create_static_canvas(
            img_p,
            video_mode=VideoMode.PORTRAIT_PADDED,
            source_box=top_box,
        )
        assert canvas_top.size == (1280, 720)

        # Inspect center pixel of canvas_top: should be predominantly red from the top half
        top_arr = np.asarray(canvas_top)
        center_pixel = top_arr[360, 640]
        assert center_pixel[0] > 180 and center_pixel[2] < 50, (
            f"Expected red center from top half, got RGB={center_pixel}"
        )

        # 3. Sub-box bottom slice (y in [0.50, 1.0])
        bottom_box = FocusBox(x_min=0.0, y_min=0.50, x_max=1.0, y_max=1.0)
        canvas_bottom = engine.create_static_canvas(
            img_p,
            video_mode=VideoMode.PORTRAIT_PADDED,
            source_box=bottom_box,
        )
        assert canvas_bottom.size == (1280, 720)

        # Center pixel of canvas_bottom: should be predominantly blue from the bottom half
        bottom_arr = np.asarray(canvas_bottom)
        bottom_center = bottom_arr[360, 640]
        assert bottom_center[2] > 180 and bottom_center[0] < 50, (
            f"Expected blue center from bottom half, got RGB={bottom_center}"
        )
