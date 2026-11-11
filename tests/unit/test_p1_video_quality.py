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
from src.render.video_engine import VideoEngine


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

class TestP1VideoModes:
    def setup_method(self):
        self.cam = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)
        self.engine = VideoEngine(target_width=1280, target_height=720, fps=30)

    def test_video_mode_landscape_fit_preserves_horizontal_content(self):
        """LANDSCAPE_FIT preserves full horizontal content, aspect-fits and pads onto canvas."""
        # Portrait panel (800x1200) forced to landscape_fit
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=800,
            img_h=1200,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC,
            video_mode=VideoMode.LANDSCAPE_FIT,
        )
        assert not is_zp
        assert "force_original_aspect_ratio=decrease" in filter_str
        assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black" in filter_str
        assert "boxblur" not in filter_str

    def test_video_mode_portrait_padded_composite(self):
        """PORTRAIT_PADDED builds dual-layer composite with blurred background wings."""
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=800,
            img_h=1200,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC,
            video_mode=VideoMode.PORTRAIT_PADDED,
        )
        assert not is_zp
        assert "split=2[bg_in][fg_in]" in filter_str
        assert "boxblur=25:5" in filter_str
        assert "overlay=(W-w)/2:(H-h)/2" in filter_str

    def test_video_mode_portrait_vertical_fit_without_distortion(self):
        """PORTRAIT_VERTICAL scales to fit vertically without horizontal stretching."""
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=600,
            img_h=1200,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC,
            video_mode=VideoMode.PORTRAIT_VERTICAL,
        )
        assert not is_zp
        assert "scale=-2:720" in filter_str
        assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black" in filter_str
        assert "boxblur" not in filter_str

    def test_video_mode_auto_routing(self):
        """AUTO mode detects aspect ratio: >= 1.3 -> LANDSCAPE_FIT, < 1.3 -> PORTRAIT_PADDED."""
        # Wide panel (1600x1000, AR = 1.6 >= 1.3)
        wide_filter, _ = self.cam.build_filterchain(
            img_w=1600,
            img_h=1000,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC,
            video_mode=VideoMode.AUTO,
        )
        assert "force_original_aspect_ratio=decrease" in wide_filter
        assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black" in wide_filter

        # Portrait panel (800x1200, AR = 0.667 < 1.3)
        port_filter, _ = self.cam.build_filterchain(
            img_w=800,
            img_h=1200,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC,
            video_mode=VideoMode.AUTO,
        )
        assert "split=2[bg_in][fg_in]" in port_filter
        assert "boxblur=25:5" in port_filter

    def test_all_video_modes_produce_distinct_filtergraphs(self):
        """Ensures landscape_fit, portrait_padded, and portrait_vertical generate distinct filtergraphs."""
        f_land, _ = self.cam.build_filterchain(
            img_w=800, img_h=1200, duration_sec=3.0, motion_type=CameraMotionType.STATIC, video_mode=VideoMode.LANDSCAPE_FIT
        )
        f_padd, _ = self.cam.build_filterchain(
            img_w=800, img_h=1200, duration_sec=3.0, motion_type=CameraMotionType.STATIC, video_mode=VideoMode.PORTRAIT_PADDED
        )
        f_vert, _ = self.cam.build_filterchain(
            img_w=800, img_h=1200, duration_sec=3.0, motion_type=CameraMotionType.STATIC, video_mode=VideoMode.PORTRAIT_VERTICAL
        )

        assert f_land != f_padd
        assert f_land != f_vert
        assert f_padd != f_vert


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

    def test_zoom_in_bounds_strictly_clamp_to_115(self):
        """ZOOM_IN zoompan expression strictly bounds maximum zoom to 1.15."""
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=1280, img_h=720, duration_sec=2.0, motion_type=CameraMotionType.ZOOM_IN
        )
        assert is_zp
        assert "1.15" in filter_str
        assert "1.25" not in filter_str
        assert "1.0+(1.15-1.0)" in filter_str or "1.15" in filter_str

    def test_zoom_out_bounds_strictly_clamp_to_115(self):
        """ZOOM_OUT zoompan expression strictly bounds start zoom to 1.15 (or aliases to STATIC in V4)."""
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=1280, img_h=720, duration_sec=2.0, motion_type=CameraMotionType.ZOOM_OUT
        )
        assert not is_zp or "1.15" in filter_str

    def test_focus_crop_bounds_strictly_enforce_min_045(self):
        """FOCUS_CROP clamps scale_ratio to >= 0.45 even when smaller focus box is requested."""
        tiny_box = FocusBox(x_min=0.45, y_min=0.45, x_max=0.55, y_max=0.55)  # Scale ~0.10
        filter_str, is_zp = self.cam.build_filterchain(
            img_w=1920,
            img_h=1080,
            duration_sec=3.0,
            motion_type=CameraMotionType.FOCUS_CROP,
            focus_box=tiny_box,
        )
        assert not is_zp
        assert "crop=" in filter_str
        # 1080 * 0.45 = 486 (even: 486)
        # 486 * (16/9) = 864
        assert "crop=864:486:" in filter_str

    def test_tall_continuous_webtoon_panel_diverts_to_static(self):
        """For tall continuous webtoon panels (H > 2W), zoom and horizontal pan divert to STATIC."""
        tall_w, tall_h = 600, 1800  # H = 3W > 2W

        # ZOOM_IN diverts to STATIC (is_zoompan is False)
        f_zoom, is_zp = self.cam.build_filterchain(
            img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type=CameraMotionType.ZOOM_IN
        )
        assert not is_zp
        assert "zoompan" not in f_zoom
        assert "boxblur=25:5" in f_zoom

        # PAN_LEFT diverts to STATIC
        f_pan_l, is_zp = self.cam.build_filterchain(
            img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type=CameraMotionType.PAN_LEFT
        )
        assert not is_zp
        assert "boxblur=25:5" in f_pan_l

        # VERTICAL_SCROLL is preserved (it's a vertical motion, safe for tall webtoons)
        f_scroll, is_zp = self.cam.build_filterchain(
            img_w=tall_w, img_h=tall_h, duration_sec=4.0, motion_type=CameraMotionType.VERTICAL_SCROLL
        )
        assert not is_zp
        assert "t/4.000" in f_scroll


# ==============================================================================
# 3. Subtitle Synchronization & Unified SRT & Vietnamese Typography
# ==============================================================================

class TestP1SubtitleSynchronization:
    def setup_method(self):
        self.sub_gen = SubtitleGenerator(max_chars_per_line=38, max_lines_per_block=2)
        self.engine = VideoEngine(target_width=1280, target_height=720, fps=30)

    def test_split_text_into_cues_proportional_duration(self):
        """Verifies split_text_into_cues apportions duration across cues proportionally."""
        long_vietnamese_text = (
            "Nhân vật chính bước vào đại sảnh của gia tộc sau mười năm xa cách. "
            "Mọi ánh mắt xung quanh đều đổ dồn về phía chàng trai trẻ với vẻ nghi hoặc và sợ hãi."
        )
        duration = 6.0
        cues = self.sub_gen.split_text_into_cues(long_vietnamese_text, duration_sec=duration)
        assert len(cues) >= 2

        # Verify monotonicity and boundaries
        assert cues[0].start_seconds == 0.0
        assert cues[-1].end_seconds == duration

        total_cue_duration = sum(c.duration_seconds for c in cues)
        assert total_cue_duration == pytest.approx(duration, abs=0.01)

        for c in cues:
            lines = c.text.split("\n")
            assert len(lines) <= 2
            for line in lines:
                assert len(line) <= 45  # Within typography limit

    def test_unified_srt_continuous_timeline_and_sync(self, tmp_path):
        """Verifies generate_srt_file generates a continuous, unified timeline."""
        segments = [
            ScriptSegment(
                seq_id="001",
                narrator_text="Sau ba năm lưu lạc giang hồ, thiếu niên cuối cùng cũng trở về cố hương.",
                duration=3.2,
            ),
            ScriptSegment(
                seq_id="002",
                narrator_text="[thở dài] Nhưng cảnh còn người mất, ngôi nhà xưa giờ chỉ là đống tro tàn hoang phế.",
                duration=4.5,
            ),
            ScriptSegment(
                seq_id="003",
                narrator_text="Một sát thủ bất ngờ lao ra từ trong bóng tối hiểm nguy!",
                duration=2.8,
            ),
        ]
        srt_path = tmp_path / "chapter_unified.srt"
        self.sub_gen.generate_srt_file(segments, srt_path)
        assert srt_path.exists()

        cues = self.sub_gen.parse_srt(srt_path)
        assert len(cues) >= 3

        # Indices must be sequential 1..N
        for idx, c in enumerate(cues):
            assert c.index == idx + 1

        # Monotonicity
        prev_end = 0.0
        for c in cues:
            assert c.start_seconds >= prev_end - 0.001
            assert c.end_seconds >= c.start_seconds
            prev_end = c.end_seconds

        total_expected = 3.2 + 4.5 + 2.8  # 10.5s
        assert self.sub_gen.validate_srt_sync(srt_path, expected_total_duration=total_expected, tolerance_seconds=0.15)

    def test_vietnamese_diacritics_overlay_rendering(self, tmp_path):
        """Verifies that all Vietnamese vowels and tone marks render onto PNG overlay."""
        vietnamese_sample = "Hoàng tử bước vào lâu đài cổ, ngắm nhìn ánh trăng vàng rực rỡ và bóng hình kẻ thù."
        out_png = tmp_path / "sub_vietnamese.png"

        result_path = self.engine.create_subtitle_overlay_png(vietnamese_sample, out_png)
        assert Path(result_path).exists()
        assert Path(result_path).stat().st_size > 1000

        with Image.open(result_path) as im:
            assert im.size == (1280, 720)
            assert im.mode == "RGBA"

    def test_generate_panel_clip_timed_cue_overlay_chaining(self, tmp_path):
        """Verifies that generate_panel_clip creates timed overlay chain for multi-cue utterances."""
        panel_img = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=4.0)
        out_clip = tmp_path / "out_clip.mp4"

        # Provide 2 distinct cues spanning [0.0, 2.0] and [2.0, 4.0]
        cues = [
            SubtitleCue(index=1, start_seconds=0.0, end_seconds=2.0, text="Câu thoại thứ nhất"),
            SubtitleCue(index=2, start_seconds=2.0, end_seconds=4.0, text="Câu thoại thứ hai tiếp theo"),
        ]

        ok = self.engine.generate_panel_clip(
            img_path=str(panel_img),
            audio_path=str(audio_wav),
            duration_ms=4000,
            output_path=str(out_clip),
            video_mode=VideoMode.PORTRAIT_PADDED,
            burn_subtitles=True,
            subtitle_cues=cues,
        )
        assert ok is True
        assert out_clip.exists()
        assert out_clip.stat().st_size > 5000

    def test_burn_subtitles_toggle(self, tmp_path):
        """Verifies that burn_subtitles=False produces video clip without subtitle overlays."""
        panel_img = make_test_image(tmp_path / "panel.jpg", width=800, height=1200)
        audio_wav = make_test_wav(tmp_path / "audio.wav", duration_sec=2.0)
        out_clean = tmp_path / "out_clean.mp4"

        ok = self.engine.generate_panel_clip(
            img_path=str(panel_img),
            audio_path=str(audio_wav),
            duration_ms=2000,
            output_path=str(out_clean),
            video_mode=VideoMode.LANDSCAPE_FIT,
            burn_subtitles=False,
            panel_text="Văn bản này không được in vào video",
        )
        assert ok is True
        assert out_clean.exists()
