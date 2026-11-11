"""Canonical V4 correctness contracts. Fixtures are unit inputs, never production evidence."""
from pathlib import Path
import copy
import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from src.core.schemas import PanelItem, PanelManifest, ScriptSegment, RecapScript, CameraMotionType
from src.core.visual_contract import Shot, VisualPlan, Region, ProductionMotion
from src.core.storage import calculate_sha256
from src.render.visual_policy import is_tall_panel, decompose_tall_panel, generate_visual_plan
from src.render.visual_plan_renderer import render_frame, frame_schedule


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / "panels").mkdir()
    panels = []
    for i, height in enumerate((200, 500, 200), 1):
        path = tmp_path / "panels" / f"p{i}.png"
        pixels = np.tile(np.arange(200, dtype=np.uint8), (height, 1))
        Image.fromarray(pixels).convert("RGB").save(path)
        panels.append(PanelItem(panel_id=f"p{i}", sequence_number=i, filename=path.name,
            relative_path=f"panels/{path.name}", source_image="page1.png", crop_box=(0, 0, 200, height),
            width=200, height=height, aspect_ratio=200/height, sha256=calculate_sha256(path),
            evidence_metadata={"text_detection_uncertain": False}))
    manifest = PanelManifest(project_slug="unit", chapter_number="1", panels=panels)
    script = RecapScript(project_slug="unit", chapter_number="1", segments=[
        ScriptSegment(seq_id="a", evidence_panel_ids=["p1", "p2"], narrator_text="unit narration", duration=2)])
    return tmp_path, manifest, script


@pytest.mark.parametrize("ratio,tall", [(2.39, False), (2.4, False), (2.41, True), (1000, True)])
def test_frozen_tall_boundary_and_monotonic_decomposition(ratio, tall):
    assert is_tall_panel(100, ratio * 100) is tall
    boxes = decompose_tall_panel("p", 100, ratio * 100)
    assert boxes[0].y_min == 0
    assert boxes[-1].y_max == pytest.approx(1)
    assert all(a.y_min < b.y_min for a, b in zip(boxes, boxes[1:]))
    assert all(a.y_max >= b.y_min for a, b in zip(boxes, boxes[1:]))
    assert (len(boxes) > 1) is tall


@pytest.mark.parametrize("motion", [m.value for m in CameraMotionType if m.value not in {p.value for p in ProductionMotion}] + ["jitter", "PAN_UP"])
def test_legacy_motion_is_rejected(motion):
    with pytest.raises(ValidationError):
        Shot(shot_id="x", segment_id="a", source_panel_id="p", duration_sec=1, motion_type=motion)


def test_narration_evidence_independent_of_shots(evidence):
    root, manifest, script = evidence
    plan = generate_visual_plan(root, manifest, script, target_width=320, target_height=180)
    assert script.segments[0].evidence_panel_ids == ["p1", "p2"]
    assert len(plan.shots) > 2  # tall evidence maps to multiple render shots
    assert sum(s.duration_sec for s in plan.shots) == pytest.approx(2)
    assert plan == generate_visual_plan(root, manifest, script, target_width=320, target_height=180)
    plan.save(root / "plan.json")
    assert VisualPlan.load(root / "plan.json") == plan


@pytest.mark.parametrize("damage", ["missing", "hash", "dimension", "unknown", "review", "duration", "no_evidence", "legacy_motion"])
def test_no_missing_evidence_or_implicit_fallback(evidence, damage):
    root, manifest, script = evidence
    panel, segment = manifest.panels[0], script.segments[0]
    if damage == "missing": (root / panel.relative_path).unlink()
    elif damage == "hash": panel.sha256 = "0" * 64
    elif damage == "dimension": panel.width = 999
    elif damage == "unknown": segment.evidence_panel_ids = ["absent"]
    elif damage == "review": segment.narrator_text = "[CAN_XAC_MINH: text]"
    elif damage == "duration": segment.duration = None
    elif damage == "no_evidence": segment.evidence_panel_ids = []
    else: segment.camera_motion = CameraMotionType.PAN_UP
    with pytest.raises((ValueError, ValidationError)):
        generate_visual_plan(root, manifest, script)


def test_substitute_prefers_same_segment_and_records_reason(evidence):
    root, manifest, script = evidence
    manifest.panels[0].notes = "text_only"
    plan = generate_visual_plan(root, manifest, script)
    shot = plan.shots[0]
    assert shot.substitute_panel_id == "p2"
    assert shot.substitution_reason == "same_segment"
    assert shot.source_panel_id == "p1"
    assert shot.source_box == Region()


def test_no_arbitrary_first_panel_substitution(evidence):
    root, manifest, script = evidence
    for panel in manifest.panels:
        panel.notes = "text_only"
    with pytest.raises(ValueError, match="No evidenced visual substitute"):
        generate_visual_plan(root, manifest, script)


def test_uncertain_text_is_conservative_full_static(evidence):
    root, manifest, script = evidence
    manifest.panels[1].evidence_metadata = {"text_detection_uncertain": True}
    plan = generate_visual_plan(root, manifest, script)
    assert len(plan.get_shots_for_panel("p2")) == 1
    assert plan.get_shots_for_panel("p2")[0].source_box == Region()


def test_avoid_region_never_aggressive_crop(evidence):
    root, manifest, script = evidence
    manifest.panels[0].evidence_metadata = dict(text_detection_uncertain=False,
        avoid_regions=[dict(x_min=0, y_min=0, x_max=0.5, y_max=0.5)],
        focus_regions=[dict(x_min=0.5, y_min=0.5, x_max=1, y_max=1)])
    shot = generate_visual_plan(root, manifest, script).shots[0]
    assert shot.source_box == Region(x_min=0.5, y_min=0.5)
    assert shot.motion_type == ProductionMotion.STATIC_FOCUS


def test_normalized_motion_is_scale_invariant():
    shot = Shot(shot_id="s", segment_id="a", source_panel_id="p", duration_sec=2,
                motion_type="slow_pan_down", scroll_fraction_per_second=0.12)
    plan = VisualPlan(chapter_number="1", shots=[shot], total_duration_sec=2, target_width=200, target_height=100)
    image = Image.fromarray(np.tile(np.arange(500, dtype=np.uint16)[:,None] % 256, (1, 200)).astype("uint8")).convert("RGB")
    a = np.array(render_frame(image, shot, plan, 0))
    b = np.array(render_frame(image, shot, plan, 1))
    assert not np.array_equal(a, b)
    with pytest.raises(ValidationError):
        Shot(**{**shot.model_dump(), "scroll_fraction_per_second": 0.12001})


def test_frame_quantization_has_no_cumulative_drift():
    shots = [Shot(shot_id=str(i), segment_id="a", source_panel_id="p", duration_sec=0.123) for i in range(100)]
    plan = VisualPlan(chapter_number="1", shots=shots, total_duration_sec=12.3)
    assert sum(count for _, count in frame_schedule(plan)) == round(12.3 * plan.fps)


@pytest.mark.parametrize("change", [dict(total_duration_sec=3), dict(shots=[]), dict(fps=0), dict(target_width=321), dict(schema_version="3.0.0")])
def test_invalid_plan_rejected(change):
    data = dict(chapter_number="1", shots=[dict(shot_id="s", segment_id="a", source_panel_id="p", duration_sec=1)], total_duration_sec=1)
    with pytest.raises(ValidationError):
        VisualPlan(**{**data, **change})


def test_evidence_legacy_alias_conflict_rejected():
    with pytest.raises(ValidationError, match="conflicts"):
        ScriptSegment(seq_id="a", narrator_text="x", panel_ids=["p1"], evidence_panel_ids=["p2"])


def test_authored_visual_plan_can_omit_evidence_without_losing_provenance(evidence):
    from src.render.visual_policy import resolve_visual_plan
    root, manifest, script = evidence
    baseline = generate_visual_plan(root, manifest, script, target_width=320, target_height=180)
    # Narration still cites p1+p2; the authored visual sequence intentionally
    # holds p1 for the full narration instead of showing every evidence panel.
    data = baseline.model_dump(mode="json")
    data["shots"] = [dict(data["shots"][0], duration_sec=2)]
    authored = VisualPlan.model_validate(data)
    authored.save(root / "visual_plan.json")
    resolved = resolve_visual_plan(root, manifest, script, target_width=320, target_height=180, fps=30, motion_enabled=None)
    assert resolved == authored
    assert script.segments[0].evidence_panel_ids == ["p1", "p2"]
    assert [s.source_panel_id for s in resolved.shots] == ["p1"]
    script.segments[0].narrator_text = "new narration"
    with pytest.raises(ValueError, match="stale"):
        resolve_visual_plan(root, manifest, script, target_width=320, target_height=180, fps=30, motion_enabled=None)
