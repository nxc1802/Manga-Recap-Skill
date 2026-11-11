"""
tests/unit/test_camera.py
Unit test suite for CameraMotionGenerator (PR-07).
"""

import pytest
from src.core.schemas import CameraMotionType, FocusBox
from src.render.camera import CameraMotionGenerator


class TestCameraMotionGenerator:
    """Tests for CameraMotionGenerator mathematics, easing, and filter expressions."""

    def setup_method(self):
        self.generator = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def test_initialization_ensures_even_dimensions(self):
        gen = CameraMotionGenerator(target_width=1281, target_height=721, fps=30)
        assert gen.target_width == 1280
        assert gen.target_height == 720
        assert gen.target_width % 2 == 0
        assert gen.target_height % 2 == 0

    def test_ensure_even_static_method(self):
        assert CameraMotionGenerator._ensure_even(100) == 100
        assert CameraMotionGenerator._ensure_even(101) == 100
        assert CameraMotionGenerator._ensure_even(0) == 0
        assert CameraMotionGenerator._ensure_even(-1) == -2

    def test_resolve_focus_coordinates_none(self):
        fx, fy, scale = self.generator.resolve_focus_coordinates(800, 600, None)
        assert fx == 0.5
        assert fy == 0.5
        assert scale == 1.0

    def test_resolve_focus_coordinates_focus_box(self):
        fb = FocusBox(x_min=0.2, y_min=0.3, x_max=0.6, y_max=0.7)
        fx, fy, scale = self.generator.resolve_focus_coordinates(800, 600, fb)
        assert pytest.approx(fx, 0.001) == 0.4
        assert pytest.approx(fy, 0.001) == 0.5
        assert pytest.approx(scale, 0.001) == 0.4

    def test_resolve_focus_coordinates_legacy_dict(self):
        legacy = {"focus_x": 70.0, "focus_y": 30.0, "crop_scale": 50.0}
        fx, fy, scale = self.generator.resolve_focus_coordinates(800, 600, legacy)
        assert pytest.approx(fx, 0.01) == 0.70
        assert pytest.approx(fy, 0.01) == 0.30
        assert pytest.approx(scale, 0.01) == 0.50

    def test_static_filter_aspect_match(self):
        # 16:9 input (1920x1080)
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1920, img_h=1080, duration_sec=3.0, motion_type=CameraMotionType.STATIC
        )
        assert not is_zoompan
        assert "scale=1280:720" in filter_str
        assert "fps=30" in filter_str
        assert "boxblur" not in filter_str

    def test_static_filter_aspect_mismatch_blurred_wings(self):
        # Square input (800x800)
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=800, img_h=800, duration_sec=3.0, motion_type=CameraMotionType.STATIC
        )
        assert not is_zoompan
        assert "boxblur=25:5" in filter_str
        assert "overlay=(W-w)/2:(H-h)/2" in filter_str

    def test_pan_right_filter(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=2400, img_h=1080, duration_sec=4.0, motion_type=CameraMotionType.PAN_RIGHT
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        assert "1-cos(PI*t/" in filter_str
        assert "scale=1280:720" in filter_str

    def test_pan_left_filter(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=2400, img_h=1080, duration_sec=4.0, motion_type=CameraMotionType.PAN_LEFT
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        assert "1+cos(PI*t/" in filter_str
        assert "scale=1280:720" in filter_str

    def test_pan_down_filter(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1080, img_h=2400, duration_sec=5.0, motion_type=CameraMotionType.PAN_DOWN
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        assert "1-cos(PI*" in filter_str and "t/5.000" in filter_str
        assert "scale=1280:720" in filter_str

    def test_pan_up_filter(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1080, img_h=2400, duration_sec=5.0, motion_type=CameraMotionType.PAN_UP
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        # In V4, PAN_UP is aliased to PAN_DOWN to enforce strictly monotonic top-to-bottom motion
        assert ("1+cos(PI*t/" in filter_str) or ("1-cos(PI*" in filter_str)
        assert "scale=1280:720" in filter_str

    def test_vertical_scroll_linear_progression(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=800, img_h=3200, duration_sec=6.0, motion_type=CameraMotionType.VERTICAL_SCROLL
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        assert "t/6.000" in filter_str
        assert "scale=1280:720" in filter_str

    def test_zoom_in_pre_crop_aspect_preservation(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=800, img_h=1200, duration_sec=3.5, motion_type=CameraMotionType.ZOOM_IN
        )
        assert is_zoompan
        assert "crop=" in filter_str
        assert "zoompan=z=" in filter_str
        assert "d=105" in filter_str  # 30 fps * 3.5s = 105 frames
        assert "s=1280x720" in filter_str

    def test_zoom_out_pre_crop_aspect_preservation(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1600, img_h=1200, duration_sec=2.0, motion_type=CameraMotionType.ZOOM_OUT
        )
        # In V4, ZOOM_OUT aliases to STATIC to eliminate reverse zoom jitter
        assert not is_zoompan
        assert "1280" in filter_str and "720" in filter_str

    def test_focus_crop_filter(self):
        fb = FocusBox(x_min=0.2, y_min=0.2, x_max=0.5, y_max=0.5)
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1920, img_h=1080, duration_sec=2.5, motion_type=CameraMotionType.FOCUS_CROP, focus_box=fb
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        assert "scale=1280:720" in filter_str

    def test_duration_clamped_to_minimum(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1280, img_h=720, duration_sec=0.1, motion_type=CameraMotionType.ZOOM_IN
        )
        # 0.1s is clamped to 0.5s -> 15 frames at 30 fps
        assert "d=15" in filter_str

    def test_invalid_motion_string_falls_back_to_static(self):
        filter_str, is_zoompan = self.generator.build_filterchain(
            img_w=1920, img_h=1080, duration_sec=2.0, motion_type="unknown_motion_type"
        )
        assert not is_zoompan
        assert "scale=1280:720" in filter_str
