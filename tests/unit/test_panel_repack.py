"""Unit tests for AI-driven panel repack (merge/split/3-group classification) and VisualPolicy integration."""

from pathlib import Path
import numpy as np
from PIL import Image
import pytest

from src.core.panel_repack import PanelPlan, PanelPlanItem, PanelPlanOp, apply_panel_plan
from src.core.schemas import PanelItem, PanelManifest, PanelStatus, RecapScript, ScriptSegment
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest
from src.core.streaming_segmenter import StreamingSegmenter
from src.core.visual_contract import ProductionMotion
from src.render.visual_policy import generate_visual_plan

pytestmark = pytest.mark.ci_l1


def _make_synthetic_chapter(tmp_path: Path) -> Path:
    chap_dir = tmp_path / "chap_1"
    raw_dir = chap_dir / "raw"
    panels_dir = chap_dir / "panels"
    raw_dir.mkdir(parents=True)
    panels_dir.mkdir(parents=True)

    rng = np.random.default_rng(7)
    # Page 1: 1080x2000 -> top gutter (0..100), block A (100..600), gutter (600..700), block B (700..1200), gutter (1200..2000)
    arr1 = np.full((2000, 1080, 3), 255, dtype=np.uint8)
    arr1[100:600] = rng.integers(20, 200, (500, 1080, 3), dtype=np.uint8)
    arr1[700:1200] = rng.integers(20, 200, (500, 1080, 3), dtype=np.uint8)
    Image.fromarray(arr1).save(raw_dir / "page_001.jpg")

    # Page 2: 1080x4400 -> tall continuous artwork (100..4300, H/W > 3.5)
    arr2 = np.full((4400, 1080, 3), 255, dtype=np.uint8)
    arr2[100:4300] = rng.integers(10, 220, (4200, 1080, 3), dtype=np.uint8)
    Image.fromarray(arr2).save(raw_dir / "page_002.jpg")

    seg = StreamingSegmenter(target_width=1080, min_gutter_height=15)
    panels = seg.segment(raw_dir, panels_dir)
    manifest = PanelManifest(
        project_slug="test_proj",
        chapter_number="1",
        total_raw_images=2,
        total_panels=len(panels),
        panels=panels,
    )
    atomic_write_json(chap_dir / "manifest.json", manifest)
    return chap_dir


def test_segmenter_keeps_tall_panel_intact_without_square_padding(tmp_path):
    chap_dir = _make_synthetic_chapter(tmp_path)
    manifest = load_manifest(chap_dir)
    assert manifest is not None
    assert len(manifest.panels) == 3  # block A, block B, and 4200px tall block C (never blind-split)
    assert manifest.panels[0].height < 1080  # no square padding
    assert manifest.panels[2].height >= 4200


def test_repack_merge_split_and_three_groups(tmp_path):
    chap_dir = _make_synthetic_chapter(tmp_path)
    manifest = load_manifest(chap_dir)
    p1, p2, p3 = [p.panel_id for p in manifest.panels]

    plan = PanelPlan(
        project_slug="test_proj",
        chapter_number="1",
        items=[
            PanelPlanItem(op=PanelPlanOp.MERGE, source_panel_ids=[p1, p2], group=PanelStatus.ACTIVE, reason="merged_scene"),
            PanelPlanItem(op=PanelPlanOp.SPLIT, source_panel_ids=[p3], group=PanelStatus.ACTIVE, split_parts=2, reason="balanced_split"),
        ],
    )
    res = apply_panel_plan(chap_dir, plan)
    assert res["panel_plan_applied"] is True
    assert res["total_panels"] == 3  # 1 merged + 2 split
    updated = load_manifest(chap_dir)
    assert updated.panel_plan_applied is True
    assert updated.panels[0].height > 1000  # merged block A + gutter + block B
    assert abs(updated.panels[1].height - updated.panels[2].height) <= 300  # balanced split


def test_repack_drop_and_context_only_sheet_filtering(tmp_path):
    chap_dir = _make_synthetic_chapter(tmp_path)
    manifest = load_manifest(chap_dir)
    p1, p2, p3 = [p.panel_id for p in manifest.panels]

    plan = PanelPlan(
        project_slug="test_proj",
        chapter_number="1",
        items=[
            PanelPlanItem(op=PanelPlanOp.DROP, source_panel_ids=[p1], group=PanelStatus.SKIP, reason="ad_banner"),
            PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=[p2], group=PanelStatus.CONTEXT_ONLY, reason="text_only"),
            PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=[p3], group=PanelStatus.ACTIVE, tall_motion_hint="scroll", reason="story_art"),
        ],
    )
    res = apply_panel_plan(chap_dir, plan)
    assert res["counts_by_group"] == {"active": 1, "context_only": 1, "skip": 1}
    updated = load_manifest(chap_dir)
    # SKIP panel must not appear on final reviewable contact sheets
    sheet_pids = [pid for s in updated.contact_sheets for pid in s.panel_ids]
    assert len(sheet_pids) == 2
    assert updated.panels[0].status == PanelStatus.SKIP
    assert updated.panels[1].status == PanelStatus.CONTEXT_ONLY
    assert updated.panels[2].status == PanelStatus.ACTIVE


def test_visual_policy_blocks_skip_and_enables_slow_pan_down_for_tall_panel(tmp_path):
    chap_dir = _make_synthetic_chapter(tmp_path)
    manifest = load_manifest(chap_dir)
    p1, p2, p3 = [p.panel_id for p in manifest.panels]
    apply_panel_plan(
        chap_dir,
        PanelPlan(
            project_slug="test_proj",
            chapter_number="1",
            items=[
                PanelPlanItem(op=PanelPlanOp.DROP, source_panel_ids=[p1], group=PanelStatus.SKIP, reason="ad"),
                PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=[p2], group=PanelStatus.CONTEXT_ONLY, reason="text_only"),
                PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=[p3], group=PanelStatus.ACTIVE, tall_motion_hint="scroll"),
            ],
        ),
    )
    updated_manifest = load_manifest(chap_dir)
    for p in updated_manifest.panels:
        p.evidence_metadata["text_detection_uncertain"] = False

    # 1. Referencing Group 3 SKIP must raise ValueError
    bad_script = RecapScript(
        project_slug="test_proj",
        chapter_number="1",
        segments=[ScriptSegment(seq_id="001", evidence_panel_ids=["panel_001"], narrator_text="bad", duration=5.0)],
    )
    with pytest.raises(ValueError, match="SKIP"):
        generate_visual_plan(chap_dir, updated_manifest, bad_script, motion_enabled=True)

    # 2. Group 2 CONTEXT_ONLY + Group 1 tall panel with motion_enabled=True -> renders Group 1 with SLOW_PAN_DOWN
    good_script = RecapScript(
        project_slug="test_proj",
        chapter_number="1",
        segments=[
            ScriptSegment(
                seq_id="001",
                evidence_panel_ids=["panel_002", "panel_003"],
                narrator_text="narration",
                duration=20.0,
            )
        ],
    )
    vplan = generate_visual_plan(chap_dir, updated_manifest, good_script, motion_enabled=True)
    # Displayed panel must only be Group 1 (panel_003) once with SLOW_PAN_DOWN (no duplicate static substitute)
    assert len(vplan.shots) == 1
    p3_shots = [s for s in vplan.shots if s.source_panel_id == "panel_003"]
    assert len(p3_shots) == 1
    assert p3_shots[0].motion_type == ProductionMotion.SLOW_PAN_DOWN
    assert 0.0 < p3_shots[0].scroll_fraction_per_second <= 0.12
    for s in vplan.shots:
        displayed_id = s.substitute_panel_id or s.source_panel_id
        assert displayed_id == "panel_003"

    # 3. Cross-segment boundary overlap + omitted-only fallback must not emit consecutive duplicates
    #    or drop adaptive motion/crop on substituted panels.
    updated_manifest.panels[0].status = PanelStatus.ACTIVE  # panel_001
    atomic_write_json(chap_dir / "manifest.json", updated_manifest)
    overlap_script = RecapScript(
        project_slug="test_proj",
        chapter_number="1",
        segments=[
            ScriptSegment(seq_id="001", evidence_panel_ids=["panel_001", "panel_003"], narrator_text="seg 1", duration=10.0),
            ScriptSegment(seq_id="002", evidence_panel_ids=["panel_003", "panel_001"], narrator_text="seg 2", duration=10.0),
        ],
    )
    oplan = generate_visual_plan(chap_dir, updated_manifest, overlap_script, motion_enabled=True)
    displayed_seq = [s.substitute_panel_id or s.source_panel_id for s in oplan.shots]
    assert displayed_seq == ["panel_001", "panel_003", "panel_001"]


def test_panel_inspect_and_split_at(tmp_path):
    from src.core.panel_repack import inspect_panels_with_candidates

    chap_dir = _make_synthetic_chapter(tmp_path)
    manifest = load_manifest(chap_dir)
    tall_pid = manifest.panels[2].panel_id

    info = inspect_panels_with_candidates(chap_dir, [tall_pid])
    assert len(info["inspected"]) == 1
    assert len(info["inspected"][0]["candidates"]) >= 1
    ratio = info["inspected"][0]["candidates"][0]["ratio"]

    p1, p2, _ = [p.panel_id for p in manifest.panels]
    res = apply_panel_plan(
        chap_dir,
        PanelPlan(
            project_slug="test_proj",
            chapter_number="1",
            items=[
                PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=[p1], group=PanelStatus.ACTIVE),
                PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=[p2], group=PanelStatus.ACTIVE),
                PanelPlanItem(op=PanelPlanOp.SPLIT_AT, source_panel_ids=[tall_pid], cut_ratios=[ratio], group=PanelStatus.ACTIVE),
            ],
        ),
    )
    assert res["total_panels"] == 4


def test_adaptive_display_box_and_simple_subtitle_chunker():
    from src.render.visual_policy import compute_adaptive_display_box
    from src.render.subtitle import chunk_vietnamese_text_sentence_aware

    # Square/landscape: no crop
    sq = compute_adaptive_display_box(1080, 1080)
    assert (sq.y_min, sq.y_max) == (0.0, 1.0)
    # Tall: 80% center (10% top, 10% bottom trimmed)
    tall = compute_adaptive_display_box(1080, 2160)
    assert (tall.y_min, tall.y_max) == (0.1, 0.9)

    # Subtitle: short/medium sentence stays 1 cue (<=2 lines); extra-long sentence splits into 2 cues
    chunks = chunk_vietnamese_text_sentence_aware(
        "Câu ngắn giữ nguyên. Đây là một câu khá dài về nhân vật chính đang chiến đấu giữa cơn mưa đêm. "
        "Còn câu thứ ba này được viết thật dài với nhiều chi tiết miêu tả nhân vật chính lao xuống từ bầu trời đêm đầy sấm chớp dữ dội.",
        max_chars_per_line=52,
        max_lines=2,
    )
    assert chunks[0] == "Câu ngắn giữ nguyên."
    assert "\n" in chunks[1]  # 2nd sentence fits cleanly as 1 two-line cue
    assert len(chunks) == 4    # 3rd sentence (>104 chars) splits into 2 balanced cues
