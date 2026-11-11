"""
tests/unit/test_visual_policy.py
Comprehensive unit test suite for Canonical Visual Policy Engine (PR-02 V4).
Tests VisualRole, Shot, VisualPlan, tall panel decomposition (0.65 <= W/H <= 2.0),
blank/text-only band detection, approved shot substitution, and canonical plan generation.
"""

import json
from pathlib import Path
from PIL import Image
import numpy as np
import pytest

from src.core.schemas import (
    CameraMotionType,
    FocusBox,
    PanelItem,
    PanelManifest,
    PanelStatus,
    RecapScript,
    ScriptSegment,
)
from src.render.visual_policy import (
    Shot,
    VisualPlan,
    VisualRole,
    decompose_tall_panel,
    detect_omit_visual_band,
    generate_visual_plan,
    is_tall_panel,
)


class TestVisualPolicyModels:
    """Tests for VisualRole, Shot, and VisualPlan data structures and validation."""

    def test_visual_role_enum_values(self):
        assert VisualRole.SHOW == "show"
        assert VisualRole.CONTEXT_ONLY == "context_only"
        assert VisualRole.OMIT_VISUAL == "omit_visual"

    def test_shot_instantiation_defaults(self):
        shot = Shot(
            shot_id="shot_001",
            source_panel_id="panel_001",
        )
        assert shot.shot_id == "shot_001"
        assert shot.source_panel_id == "panel_001"
        assert shot.visual_role == VisualRole.SHOW
        assert shot.visual_omit_reason is None
        assert shot.substitute_panel_id is None
        assert shot.motion_type == CameraMotionType.STATIC
        assert shot.speed_px_per_sec == 0.0
        assert shot.duration_sec == 0.0
        assert shot.source_box.x_min == 0.0
        assert shot.source_box.y_max == 1.0

    def test_shot_omit_visual_with_reason_and_substitute(self):
        shot = Shot(
            shot_id="shot_002",
            source_panel_id="panel_002",
            visual_role=VisualRole.OMIT_VISUAL,
            visual_omit_reason="blank_band",
            substitute_panel_id="panel_001",
            duration_sec=2.5,
        )
        assert shot.visual_role == VisualRole.OMIT_VISUAL
        assert shot.visual_omit_reason == "blank_band"
        assert shot.substitute_panel_id == "panel_001"
        assert shot.duration_sec == 2.5

    def test_visual_plan_save_and_load_roundtrip(self, tmp_path):
        shot1 = Shot(
            shot_id="shot_001",
            source_panel_id="panel_001",
            visual_role=VisualRole.SHOW,
            source_box=FocusBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=0.55),
            duration_sec=3.0,
            segment_id="001",
        )
        shot2 = Shot(
            shot_id="shot_002",
            source_panel_id="panel_001",
            visual_role=VisualRole.SHOW,
            source_box=FocusBox(x_min=0.0, y_min=0.45, x_max=1.0, y_max=1.0),
            duration_sec=3.0,
            segment_id="001",
        )
        plan = VisualPlan(
            chapter_number="1",
            shots=[shot1, shot2],
            panel_to_shots_map={"panel_001": ["shot_001", "shot_002"]},
            total_duration_sec=6.0,
        )

        plan_file = tmp_path / "visual_plan.json"
        saved_p = plan.save(plan_file)
        assert saved_p.exists()

        loaded_plan = VisualPlan.load(plan_file)
        assert loaded_plan.chapter_number == "1"
        assert len(loaded_plan.shots) == 2
        assert loaded_plan.total_duration_sec == 6.0
        assert loaded_plan.panel_to_shots_map["panel_001"] == ["shot_001", "shot_002"]

        # Helper method lookups
        seg_shots = loaded_plan.get_shots_for_segment("001")
        assert len(seg_shots) == 2
        panel_shots = loaded_plan.get_shots_for_panel("panel_001")
        assert len(panel_shots) == 2


class TestTallPanelDecomposition:
    """Tests for tall panel detection and bounded sub-panel slicing (0.65 <= W/H <= 2.0)."""

    def test_is_tall_panel_threshold(self):
        # 1080x1080 (AR = 1.0, H/W = 1.0) -> Not tall
        assert not is_tall_panel(1080, 1080, threshold_h_over_w=2.0)
        # 1080x2160 (AR = 0.5, H/W = 2.0) -> Not tall (at boundary)
        assert not is_tall_panel(1080, 2160, threshold_h_over_w=2.0)
        # 1080x2161 (H/W > 2.0) -> Tall
        assert is_tall_panel(1080, 2161, threshold_h_over_w=2.0)
        # 1080x3500 (H/W = 3.24) -> Tall
        assert is_tall_panel(1080, 3500, threshold_h_over_w=2.0)

    def test_decompose_normal_panel_returns_full_box(self):
        boxes = decompose_tall_panel("panel_001", width=1080, height=1500)
        assert len(boxes) == 1
        assert boxes[0].x_min == 0.0
        assert boxes[0].y_min == 0.0
        assert boxes[0].x_max == 1.0
        assert boxes[0].y_max == 1.0

    def test_decompose_tall_panel_aspect_ratio_bounds(self):
        # Test varying tall heights (2.4x, 3.0x, 4.0x width)
        for h_mult in [2.4, 3.0, 4.0]:
            width = 1080
            height = int(round(width * h_mult))
            boxes = decompose_tall_panel("tall_panel", width=width, height=height)

            # Must have at least 2 slices
            assert len(boxes) >= 2, f"Failed for h_mult={h_mult}"

            # Check each slice properties
            for i, box in enumerate(boxes):
                sub_w_px = width * (box.x_max - box.x_min)
                sub_h_px = height * (box.y_max - box.y_min)
                sub_ar = sub_w_px / sub_h_px

                # Requirement: 0.65 <= W/H <= 2.0
                assert 0.65 <= sub_ar <= 2.0, (
                    f"Slice {i} aspect ratio {sub_ar:.3f} outside [0.65, 2.0] for H/W={h_mult}"
                )

                # FocusBox constraints: w >= 0.35, h >= 0.05
                assert (box.x_max - box.x_min) >= 0.35
                assert (box.y_max - box.y_min) >= 0.05

            # Verify strictly monotonic top-to-bottom reading progression
            for i in range(len(boxes) - 1):
                assert boxes[i + 1].y_min > boxes[i].y_min, "Slices must strictly progress downward"
                assert boxes[i + 1].y_max > boxes[i].y_max, "Slices must strictly progress downward"
                # Verify overlap between consecutive slices (no gap)
                assert boxes[i].y_max > boxes[i + 1].y_min, (
                    f"Missing overlap between slice {i} and {i+1}"
                )

            # First slice starts at top, last slice ends at bottom
            assert boxes[0].y_min == 0.0
            assert boxes[-1].y_max == 1.0


class TestDetectOmitVisualBand:
    """Tests for classifying blank bands, tiny slivers, and text-only content."""

    def test_detect_blank_white_band(self, tmp_path):
        # Solid white image (variance = 0)
        img_p = tmp_path / "blank_white.jpg"
        im = Image.new("RGB", (1080, 500), (255, 255, 255))
        im.save(img_p)

        is_omit, reason = detect_omit_visual_band(img_p)
        assert is_omit is True
        assert reason == "blank_band"

    def test_detect_blank_black_band(self, tmp_path):
        # Solid black image (variance = 0)
        img_p = tmp_path / "blank_black.jpg"
        im = Image.new("RGB", (1080, 500), (0, 0, 0))
        im.save(img_p)

        is_omit, reason = detect_omit_visual_band(img_p)
        assert is_omit is True
        assert reason == "blank_band"

    def test_detect_sliver_below_80px(self, tmp_path):
        # High-contrast 40px sliver
        img_p = tmp_path / "sliver.jpg"
        arr = np.zeros((40, 1080, 3), dtype=np.uint8)
        arr[:20, :, :] = 255
        im = Image.fromarray(arr)
        im.save(img_p)

        is_omit, reason = detect_omit_visual_band(img_p)
        assert is_omit is True
        assert reason == "sliver"

    def test_detect_manifest_skipped_panel(self):
        panel = PanelItem(
            panel_id="panel_005",
            sequence_number=5,
            filename="panel_005.jpg",
            relative_path="panels/panel_005.jpg",
            source_image="raw.jpg",
            crop_box=(0, 0, 1080, 200),
            width=1080,
            height=200,
            aspect_ratio=5.4,
            sha256="dummy",
            status=PanelStatus.SKIP,
        )
        is_omit, reason = detect_omit_visual_band(panel_item=panel)
        assert is_omit is True
        assert reason == "blank_band"

    def test_active_artwork_not_omitted(self, tmp_path):
        # Rich varied artwork
        img_p = tmp_path / "artwork.jpg"
        rng = np.random.default_rng(42)
        arr = rng.integers(30, 220, (1080, 1080, 3), dtype=np.uint8)
        im = Image.fromarray(arr)
        im.save(img_p)

        panel = PanelItem(
            panel_id="panel_001",
            sequence_number=1,
            filename="panel_001.jpg",
            relative_path="panels/panel_001.jpg",
            source_image="raw.jpg",
            crop_box=(0, 0, 1080, 1080),
            width=1080,
            height=1080,
            aspect_ratio=1.0,
            sha256="dummy",
            status=PanelStatus.ACTIVE,
        )
        is_omit, reason = detect_omit_visual_band(img_p, panel_item=panel)
        assert is_omit is False
        assert reason is None


class TestGenerateVisualPlan:
    """End-to-end canonical visual plan generation test with tall panels and omit_visual."""

    def test_generate_visual_plan_decomposition_and_substitution(self, tmp_path):
        chap_dir = tmp_path / "chapters" / "1"
        panels_dir = chap_dir / "panels"
        panels_dir.mkdir(parents=True, exist_ok=True)

        # Panel 1: Normal artwork (1080x1200) -> approved visual
        p1 = panels_dir / "panel_001.jpg"
        arr1 = np.random.default_rng(1).integers(20, 200, (1200, 1080, 3), dtype=np.uint8)
        Image.fromarray(arr1).save(p1)

        # Panel 2: Tall panel (1080x2800, H/W = 2.59 > 2.0)
        p2 = panels_dir / "panel_002.jpg"
        arr2 = np.random.default_rng(2).integers(20, 200, (2800, 1080, 3), dtype=np.uint8)
        Image.fromarray(arr2).save(p2)

        # Panel 3: Blank band (1080x300, solid white) -> omit_visual
        p3 = panels_dir / "panel_003.jpg"
        Image.new("RGB", (1080, 300), (255, 255, 255)).save(p3)

        manifest = PanelManifest(
            project_slug="test_proj",
            chapter_number="1",
            total_panels=3,
            panels=[
                PanelItem(
                    panel_id="panel_001",
                    sequence_number=1,
                    filename="panel_001.jpg",
                    relative_path="panels/panel_001.jpg",
                    source_image="page_1.jpg",
                    crop_box=(0, 0, 1080, 1200),
                    width=1080,
                    height=1200,
                    aspect_ratio=0.9,
                    sha256="h1",
                    status=PanelStatus.ACTIVE,
                ),
                PanelItem(
                    panel_id="panel_002",
                    sequence_number=2,
                    filename="panel_002.jpg",
                    relative_path="panels/panel_002.jpg",
                    source_image="page_1.jpg",
                    crop_box=(0, 1200, 1080, 4000),
                    width=1080,
                    height=2800,
                    aspect_ratio=0.386,
                    sha256="h2",
                    status=PanelStatus.ACTIVE,
                ),
                PanelItem(
                    panel_id="panel_003",
                    sequence_number=3,
                    filename="panel_003.jpg",
                    relative_path="panels/panel_003.jpg",
                    source_image="page_2.jpg",
                    crop_box=(0, 0, 1080, 300),
                    width=1080,
                    height=300,
                    aspect_ratio=3.6,
                    sha256="h3",
                    status=PanelStatus.ACTIVE,
                ),
            ],
        )

        script = RecapScript(
            project_slug="test_proj",
            chapter_number="1",
            title="Episode 1",
            segments=[
                ScriptSegment(
                    seq_id="001",
                    panel_ids=["panel_001.jpg"],
                    narrator_text="Mở đầu câu chuyện tại kinh thành.",
                    camera_motion=CameraMotionType.STATIC,
                    duration=2.0,
                ),
                ScriptSegment(
                    seq_id="002",
                    panel_ids=["panel_002.jpg"],
                    narrator_text="Đại hiệp tung mình từ trên vách núi xuống vực thẳm.",
                    camera_motion=CameraMotionType.PAN_DOWN,
                    duration=6.0,
                ),
                ScriptSegment(
                    seq_id="003",
                    panel_ids=["panel_003.jpg"],
                    narrator_text="Không ai có thể ngờ tới kết cục này.",
                    camera_motion=CameraMotionType.STATIC,
                    duration=2.5,
                ),
            ],
            total_duration_seconds=10.5,
        )

        plan = generate_visual_plan(
            chap_dir=chap_dir,
            manifest=manifest,
            script=script,
            target_width=1920,
            target_height=1080,
            fps=30,
        )

        assert plan.chapter_number == "1"
        assert plan.target_width == 1920
        assert plan.target_height == 1080

        # Segment 1: single panel_001 shot
        shots_seg1 = plan.get_shots_for_segment("001")
        assert len(shots_seg1) == 1
        assert shots_seg1[0].source_panel_id == "panel_001"
        assert shots_seg1[0].visual_role == VisualRole.SHOW

        # Segment 2: tall panel_002 auto-split into at least 2 sub-shots
        shots_seg2 = plan.get_shots_for_segment("002")
        assert len(shots_seg2) >= 2
        for s in shots_seg2:
            assert s.source_panel_id == "panel_002"
            assert s.visual_role == VisualRole.SHOW
            # Speed capped at <= 120 px/s
            assert s.speed_px_per_sec <= 120.0

        # Segment 3: blank panel_003 classified as OMIT_VISUAL with substitution
        shots_seg3 = plan.get_shots_for_segment("003")
        assert len(shots_seg3) == 1
        assert shots_seg3[0].source_panel_id == "panel_003"
        assert shots_seg3[0].visual_role == VisualRole.OMIT_VISUAL
        assert shots_seg3[0].visual_omit_reason == "blank_band"
        # Must substitute an approved panel (e.g. panel_001)
        assert shots_seg3[0].substitute_panel_id in ("panel_001", "panel_002")
