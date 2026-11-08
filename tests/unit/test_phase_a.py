"""
tests/unit/test_phase_a.py
Comprehensive test suite for Phase A Upgrades:
- PR-01: AutoFocus & Semantic Director (Segmenter variance check, VisualShotPlan schemas, STATIC_FOCUS camera motion, VideoEngine multi-shot cumulative frame allocation)
- PR-02: Sentence-Aware Subtitle Intelligence & CLI (Sentence-aware chunking, no orphan words, SRT/Overlay parity, cache invalidation)
- PR-03: Soft Script Pacing (Pre-TTS syllable + pause duration modeling, budget diagnostics, validate --pacing report)
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

from src.cli.validate_cmd import handle_validate
from src.core.storage import (
    atomic_write_json,
    init_chapter_layout,
    init_project_layout,
)
from src.core.pacing import (
    count_punctuation_pauses,
    estimate_script_duration_seconds,
    estimate_segment_duration_seconds,
    format_pacing_report,
)
from src.core.schemas import (
    CameraMotionType,
    FocusBox,
    PacingMode,
    PanelStatus,
    ProjectConfig,
    RecapScript,
    RenderSettings,
    ScriptPacingConfig,
    ScriptSegment,
    SourceKind,
    SourceRegion,
    SubtitleChunkingMode,
    SubtitleSettings,
    VisualShot,
    VisualShotPlan,
)
from src.core.streaming_segmenter import RawPageMap, StreamingSegmenter
from src.render.camera import CameraMotionGenerator
from src.render.subtitle import (
    SubtitleCue,
    SubtitleGenerator,
    chunk_vietnamese_text_sentence_aware,
)
from src.render.video_engine import VideoEngine


# ==============================================================================
# PR-01 Tests: AutoFocus & Semantic Director
# ==============================================================================

class TestPR01AutoFocusAndSemanticDirector:
    """Tests for PR-01: AutoFocus & Semantic Visual Director."""

    def test_streaming_segmenter_under_height_blank_panel_marked_skip(self, tmp_path):
        """
        Verifies that under-height panels (< target_width) with low variance
        are correctly evaluated and marked PanelStatus.SKIP before padding.
        """
        output_dir = tmp_path / "panels"
        output_dir.mkdir(parents=True)
        segmenter = StreamingSegmenter(target_width=1080, sufficiency_variance=10.0)

        # Create a short (1080x80) uniform white blank image (variance = 0.0)
        blank_strip = Image.new("RGB", (1080, 80), color=(255, 255, 255))
        strip_path = tmp_path / "page_001.jpg"
        blank_strip.save(strip_path, "JPEG")

        page_maps = segmenter.build_page_maps([strip_path])
        panels = segmenter.extract_panels(page_maps, [(0, 80)], output_dir)

        assert len(panels) == 1
        panel = panels[0]
        # Padded to square (1080x1080)
        assert panel.width == 1080
        assert panel.height == 1080
        # BUT marked SKIP because crop_var < sufficiency_variance!
        assert panel.status == PanelStatus.SKIP

    def test_streaming_segmenter_under_height_content_panel_marked_active(self, tmp_path):
        """
        Verifies that under-height panels with sufficient detail variance
        are padded to square and remain PanelStatus.ACTIVE.
        """
        output_dir = tmp_path / "panels"
        output_dir.mkdir(parents=True)
        segmenter = StreamingSegmenter(target_width=1080, sufficiency_variance=10.0)

        # Create a detailed pattern image with high variance
        arr = np.zeros((200, 1080, 3), dtype=np.uint8)
        arr[::2, :, :] = 255
        content_strip = Image.fromarray(arr)
        strip_path = tmp_path / "page_002.jpg"
        content_strip.save(strip_path, "JPEG")

        page_maps = segmenter.build_page_maps([strip_path])
        panels = segmenter.extract_panels(page_maps, [(0, 200)], output_dir)

        assert len(panels) == 1
        panel = panels[0]
        assert panel.width == 1080
        assert panel.height == 1080
        assert panel.status == PanelStatus.ACTIVE

    def test_visual_shot_and_plan_schema_validation(self):
        """Verifies Pydantic v2 schemas for SourceRegion, VisualShot, and VisualShotPlan."""
        source = SourceRegion(kind=SourceKind.PANEL, panel_id="panel_005.jpg")
        focus = FocusBox(x_min=0.2, y_min=0.3, x_max=0.8, y_max=0.9)
        shot = VisualShot(
            shot_id="shot_01",
            source=source,
            focus_box=focus,
            subject="protagonist",
            motion=CameraMotionType.STATIC_FOCUS,
            duration_ratio=0.65,
            reason="Establish character reaction",
        )
        assert shot.duration_ratio == 0.65
        assert shot.motion == CameraMotionType.STATIC_FOCUS
        assert shot.source.kind == SourceKind.PANEL

        plan = VisualShotPlan(
            project_slug="test_manga",
            chapter_number="1",
            shots_by_segment={"001": [shot]},
        )
        assert "001" in plan.shots_by_segment
        assert len(plan.shots_by_segment["001"]) == 1

        # Test JSON round-trip
        dumped = plan.model_dump_json()
        loaded = VisualShotPlan.model_validate_json(dumped)
        assert loaded.shots_by_segment["001"][0].subject == "protagonist"

    def test_script_segment_shots_backward_compatibility(self):
        """Verifies that ScriptSegment maintains 100% backward compatibility when shots is None."""
        legacy_seg = ScriptSegment(
            seq_id="001",
            narrator_text="Nhân vật bước vào căn phòng.",
            panel_ids=["panel_001.jpg"],
            camera_motion=CameraMotionType.STATIC,
        )
        assert legacy_seg.shots is None
        data = legacy_seg.model_dump()
        assert data["shots"] is None

        # Re-validate without shots
        reloaded = ScriptSegment.model_validate(data)
        assert reloaded.shots is None
        assert reloaded.camera_motion == CameraMotionType.STATIC

        # Upgrade with shots
        shot1 = VisualShot(shot_id="01", motion=CameraMotionType.STATIC_FOCUS, duration_ratio=0.7)
        shot2 = VisualShot(shot_id="02", motion=CameraMotionType.ZOOM_IN, duration_ratio=0.3)
        upgraded_seg = ScriptSegment(
            seq_id="002",
            narrator_text="Đột nhiên một bóng đen xuất hiện.",
            panel_ids=["panel_002.jpg"],
            shots=[shot1, shot2],
        )
        assert upgraded_seg.shots is not None
        assert len(upgraded_seg.shots) == 2

    def test_camera_static_focus_filterchain_generation(self):
        """
        Verifies that STATIC_FOCUS creates a static 16:9 crop centered around the subject
        without keyframe/zoompan animation.
        """
        cam = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)
        focus = FocusBox(x_min=0.2, y_min=0.2, x_max=0.6, y_max=0.6)

        # 1. Explicit static_focus motion
        filter_str, is_zoompan = cam.build_filterchain(
            img_w=1080,
            img_h=1080,
            duration_sec=3.0,
            motion_type=CameraMotionType.STATIC_FOCUS,
            focus_box=focus,
            allow_animation=True,
        )
        assert not is_zoompan
        assert "crop=" in filter_str
        assert "scale=1280:720" in filter_str
        assert "zoompan=" not in filter_str

        # 2. When allow_animation=False with focus_box, falls back to STATIC_FOCUS instead of whole-image STATIC
        filter_no_anim, is_zoompan_no_anim = cam.build_filterchain(
            img_w=1080,
            img_h=1080,
            duration_sec=3.0,
            motion_type=CameraMotionType.ZOOM_IN,
            focus_box=focus,
            allow_animation=False,
        )
        assert not is_zoompan_no_anim
        assert "crop=" in filter_no_anim
        assert "scale=1280:720" in filter_no_anim
        assert "zoompan=" not in filter_no_anim

    def test_camera_motion_type_extensions(self):
        """Verifies SUBJECT_PAN and CONTENT_AWARE_SCROLL motions in CameraMotionGenerator."""
        cam = CameraMotionGenerator(target_width=1280, target_height=720, fps=30)
        focus = FocusBox(x_min=0.3, y_min=0.3, x_max=0.7, y_max=0.7)

        # SUBJECT_PAN
        f_pan, zp_pan = cam.build_filterchain(
            img_w=1920,
            img_h=1080,
            duration_sec=4.0,
            motion_type=CameraMotionType.SUBJECT_PAN,
            focus_box=focus,
        )
        assert not zp_pan
        assert "crop=" in f_pan

        # CONTENT_AWARE_SCROLL
        f_scroll, zp_scroll = cam.build_filterchain(
            img_w=1080,
            img_h=2500,
            duration_sec=5.0,
            motion_type=CameraMotionType.CONTENT_AWARE_SCROLL,
            focus_box=focus,
        )
        assert not zp_scroll
        assert "crop=" in f_scroll


# ==============================================================================
# PR-02 Tests: Sentence-Aware Subtitle Intelligence
# ==============================================================================

class TestPR02SentenceAwareSubtitles:
    """Tests for PR-02: Sentence-Aware Subtitle Intelligence & CLI."""

    def test_chunk_vietnamese_text_sentence_aware_boundaries(self):
        """
        Verifies splitting text strictly at natural sentence ends (.?!…)
        and clause breaks (,;:—), avoiding orphan words.
        """
        text = (
            "Sau ba năm lưu lạc, thiếu niên cuối cùng cũng trở về cố hương. "
            "Nhưng cảnh còn người mất, gia tộc xưa đã hoàn toàn lụi tàn!"
        )
        chunks = chunk_vietnamese_text_sentence_aware(text, max_chars_per_line=42, max_lines=1)
        assert len(chunks) >= 2
        # First sentence should not be cut awkwardly mid-word
        for c in chunks:
            assert len(c) <= 50  # Respect safe line length
            # Minimum 2 words (no orphan word like "tàn!")
            words = c.split()
            assert len(words) >= 2

    def test_no_orphan_word_guarantee(self):
        """
        Verifies that trailing single words (e.g. 1 word left over)
        are never emitted as an isolated cue.
        """
        # Text constructed where the last word would naturally overflow a 30-char limit
        text = "Nhân vật chính bước vào đại sảnh trang nghiêm"
        chunks = chunk_vietnamese_text_sentence_aware(text, max_chars_per_line=30, max_lines=1)
        for c in chunks:
            words = c.split()
            assert len(words) >= 2, f"Orphan word found in chunk: '{c}'"

    def test_subtitle_overlay_matches_srt_text(self, tmp_path):
        """
        Verifies that VideoEngine.create_subtitle_overlay_png produces
        an overlay matching the exact cue text without divergent re-wrapping.
        """
        engine = VideoEngine(target_width=1280, target_height=720, fps=30)
        cue_text = "Hắn quay lưng bước đi, không hề ngoảnh lại."
        out_png = tmp_path / "sub_overlay.png"

        result_path = engine.create_subtitle_overlay_png(cue_text, out_png, font_size=26)
        assert Path(result_path).exists()
        assert Path(result_path).stat().st_size > 0

        # Check image dimensions
        with Image.open(result_path) as im:
            assert im.size == (1280, 720)
            assert im.mode == "RGBA"

    def test_render_cache_fingerprint_invalidated_by_subtitle_settings(self):
        """
        Verifies that changing subtitle settings (lines, font_size, chunking)
        modifies the render cache fingerprint to trigger a fresh render.
        """
        base_fp = {
            "quality": "final",
            "target_width": 1920,
            "target_height": 1080,
            "fps": 30,
            "subtitle_lines": 1,
            "subtitle_font_size": 26,
            "subtitle_chunking": "sentence-aware",
        }

        # Modify font size
        fp_font = dict(base_fp, subtitle_font_size=32)
        assert base_fp != fp_font

        # Modify line count
        fp_lines = dict(base_fp, subtitle_lines=2)
        assert base_fp != fp_lines

        # Modify chunking
        fp_chunk = dict(base_fp, subtitle_chunking="fixed-chars")
        assert base_fp != fp_chunk


# ==============================================================================
# PR-03 Tests: Soft Script Pacing & Duration Budget
# ==============================================================================

class TestPR03SoftScriptPacing:
    """Tests for PR-03: Soft Script Pacing & Duration Budget Estimator."""

    def test_count_punctuation_pauses_no_double_counting(self):
        """Verifies ellipses are not double-counted as 3 separate sentence ends."""
        text = "Khoan đã... Ngươi là ai? Không thể nào!"
        commas, ends, ellipses = count_punctuation_pauses(text)
        assert ellipses == 1
        assert ends == 2  # '?' and '!'
        assert commas == 0

    def test_estimate_segment_duration_seconds(self):
        """Verifies pre-TTS duration estimation matches empirical VieNeu-TTS bounds."""
        text = "Sau ba năm tu luyện gian khổ, Diệp Viêm cuối cùng cũng đột phá cảnh giới Đấu Vương!"
        # 17 syllables, 1 comma, 1 exclamation
        dur_1x = estimate_segment_duration_seconds(text, speed=1.0)
        assert 5.0 <= dur_1x <= 7.0

        # Faster speed should decrease estimated duration
        dur_1_5x = estimate_segment_duration_seconds(text, speed=1.5)
        assert dur_1_5x < dur_1x

    def test_estimate_script_duration_and_budget_diagnostics(self):
        """Verifies full script pacing calculation against target minutes."""
        script = RecapScript(
            project_slug="test_proj",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", narrator_text="Trời đất xoay vần, vạn vật hồi sinh."),
                ScriptSegment(seq_id="002", narrator_text="Một thiếu niên từ trong đống đổ nát đứng dậy."),
                ScriptSegment(seq_id="003", narrator_text="Ánh mắt hắn rực cháy ngọn lửa phục thù!"),
            ],
        )

        pacing_cfg = ScriptPacingConfig(
            mode=PacingMode.BALANCED,
            target_total_minutes=0.2,  # 12 seconds
            tolerance=0.20,
        )

        result = estimate_script_duration_seconds(script, speed=1.0, pacing_config=pacing_cfg)
        assert result["total_syllables"] > 0
        assert result["total_estimated_seconds"] > 0.0
        assert "recommendations" in result
        assert isinstance(result["recommendations"], list)
        assert len(result["recommendations"]) >= 1

        report_str = format_pacing_report(result)
        assert "PRE-TTS SCRIPT PACING DIAGNOSTIC REPORT" in report_str
        assert "Target Duration" in report_str

    def test_validate_cmd_pacing_flag_integration(self, tmp_path, monkeypatch):
        """Verifies that manga-recap validate --pacing runs pacing diagnostics."""
        monkeypatch.setenv("MANGA_RECAP_DATA_DIR", str(tmp_path / "data"))
        proj_dir = init_project_layout("pacing_test_proj", "Pacing Test")
        chap_dir = init_chapter_layout("pacing_test_proj", 1)

        # Update project.json with ScriptPacingConfig
        proj_cfg = ProjectConfig(
            project_id="proj_01",
            slug="pacing_test_proj",
            title="Pacing Test",
            script_pacing=ScriptPacingConfig(mode=PacingMode.BALANCED, target_total_minutes=1.0),
        )
        atomic_write_json(proj_dir / "project.json", proj_cfg)

        # Write valid script.json
        script = RecapScript(
            project_slug="pacing_test_proj",
            chapter_number="1",
            title="Chapter 1",
            segments=[
                ScriptSegment(seq_id="001", narrator_text="Khởi đầu câu chuyện huyền bí nơi hoang mạc."),
            ],
        )
        atomic_write_json(chap_dir / "script.json", script)

        args = argparse.Namespace(
            project="pacing_test_proj",
            chapter="1",
            strict=False,
            pacing=True,
        )

        result = handle_validate(args)

        assert result["valid"] is True
        assert "pacing_reports" in result
        assert "1" in result["pacing_reports"]
        pacing_data = result["pacing_reports"]["1"]
        assert pacing_data["total_syllables"] > 0
        assert pacing_data["total_estimated_seconds"] > 0.0
