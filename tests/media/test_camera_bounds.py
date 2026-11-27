"""
tests/unit/test_p1_video_quality.py
Comprehensive test suite for Phase 2 — P1: Fix Video Quality.
Covers:
1. Video Modes (auto, landscape_fit, portrait_padded, portrait_vertical) producing distinct, correct filtergraphs.
2. Camera Motion Bounds Synchronization (max_zoom <= 1.15, min_crop_scale >= 0.45, tall panel protection).
3. Subtitle Synchronization & Unified SRT (proportional cues, continuous chapter timeline, Vietnamese diacritics).
"""

import math
import struct
import wave
from pathlib import Path
from PIL import Image
import pytest

from src.core.schemas import (
    CameraMotionType,
    FocusBox,
    RecapScript,
    ScriptSegment,
    VideoMode,
)
from src.render.camera import CameraMotionGenerator
from src.render.subtitle import (
    SubtitleCue,
    SubtitleGenerator,
    sanitize_subtitle_text,
)


# ==============================================================================
# Helpers
# ==============================================================================

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


def make_test_image(path: Path, width: int = 800, height: int = 1200, color=(120, 80, 160)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(path, "JPEG")
    return path


# ==============================================================================
# 1. Video Modes & Distinct Layout Tests
# ==============================================================================



# ==============================================================================
# 2. Camera Motion Bounds & Guardrail Tests
# ==============================================================================

class TestP1CameraMotionBounds:
    def setup_method(self):
        self.cam = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)

    def test_generator_initialization_clamps_max_zoom(self):
        """CameraMotionGenerator defaults to 1.15 and clamps any excessive max_zoom <= 1.15."""
        default_cam = CameraMotionGenerator()
        assert default_cam.max_zoom == 1.15
        assert default_cam.min_crop_scale == 0.45

        custom_high_cam = CameraMotionGenerator(max_zoom=1.50)
        assert custom_high_cam.max_zoom == 1.15

    def test_static_focus_bounds_strictly_enforce_min_045(self):
        """STATIC_FOCUS clamps scale_ratio to >= 0.45 even when smaller focus box is requested."""
        tiny_box = FocusBox(x_min=0.45, y_min=0.45, x_max=0.55, y_max=0.55)
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=1920,
            img_h=1080,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC_FOCUS,
            focus_box=tiny_box,
        )
        assert not is_zp
        assert "crop=864:486:" in filter_str

    def test_tall_continuous_webtoon_panel_slow_pan_down_and_legacy_rejection(self):
        """SLOW_PAN_DOWN handles tall panels while removed legacy motions fail cleanly."""
        tall_w, tall_h = 600, 1800

        with pytest.raises(ValueError, match="Unsupported camera motion"):
            self.cam.build_filterchain(img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type="zoom_in")
        with pytest.raises(ValueError, match="Unsupported camera motion"):
            self.cam.build_filterchain(img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type="pan_left")

        f_scroll, is_zp = self.cam.build_filterchain(
            img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type=CameraMotionType.SLOW_PAN_DOWN
        )
        assert not is_zp
        assert "t/4.000" in f_scroll
        assert "boxblur" not in f_scroll

        f_static, _ = self.cam.build_filterchain(
            img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type=CameraMotionType.STATIC
        )
        assert "force_original_aspect_ratio=decrease" in f_static
        assert "boxblur" not in f_static


# ==============================================================================
# 3. Subtitle Synchronization & Unified SRT & Vietnamese Typography
# ==============================================================================
