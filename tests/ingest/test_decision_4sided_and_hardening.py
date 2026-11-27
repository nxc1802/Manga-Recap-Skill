"""Comprehensive tests for 4-sided Decision contracts, guardrails, repack transactions, and render safety."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any, Dict, List

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from src.core.errors import ProviderIntegrityError
from src.core.panel_repack import PanelPlan, PanelPlanItem, PanelPlanOp, apply_panel_plan
from src.core.project import init_chapter_layout, init_project_layout
from src.core.schemas import (
    PanelItem,
    PanelManifest,
    PanelStatus,
    RecapScript,
    ScriptSegment,
)
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest, read_json
from src.core.visual_contract import ProductionMotion, Region, VisualPlan
from src.ingest.decision import (
    CROP_PCT_OPTIONS,
    DecisionPolicyConfig,
    compute_deterministic_motion,
    evaluate_4sided_crop,
    run_decision_pipeline,
)
from src.providers.decision import DecisionClient, ProviderContentBlockedError
from src.render.fast_renderer import render_frame
from src.render.visual_policy import generate_visual_plan, validate_visual_plan


def _save_marked_panel(
    path: Path,
    width: int,
    height: int,
    *,
    border_pct: float = 0.0,
    draw_gutter: bool = False,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (width, height), color=(240, 240, 240))
    draw = ImageDraw.Draw(im)
    if border_pct > 0.0:
        bx = int(round(width * border_pct))
        by = int(round(height * border_pct))
        draw.rectangle([0, 0, width - 1, height - 1], fill=(220, 40, 40))
        draw.rectangle([bx, by, width - bx - 1, height - by - 1], fill=(245, 245, 250))
        draw.rectangle([bx + 20, by + 20, width - bx - 21, height - by - 21], fill=(20, 60, 220))
    else:
        for x in range(0, width, 40):
            color = (20, 30, 80) if (x // 40) % 2 == 0 else (200, 140, 60)
            draw.rectangle([x, 0, min(width, x + 20), height], fill=color)
    if draw_gutter:
        mid = height // 2
        draw.rectangle([0, mid - 50, width, mid + 50], fill=(255, 255, 255))
    im.save(path, format="JPEG", quality=92)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_manifest_chapter(tmp_path: Path, monkeypatch, specs: List[Dict[str, Any]]) -> Path:
    monkeypatch.setenv("DATA_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("DATA_OUTPUT_DIR", str(tmp_path / "output"))
    init_project_layout("dec4-proj", "Four Sided Test Series")
    chap_dir = init_chapter_layout("dec4-proj", "1")
    items: List[PanelItem] = []
    vcs_cursor = 0
    for idx, spec in enumerate(specs, start=1):
        pid = spec["panel_id"]
        w, h = int(spec["width"]), int(spec["height"])
        raw_name = f"{idx:03d}.png"
        raw_path = chap_dir / "raw" / raw_name
        _save_marked_panel(raw_path, w, h, draw_gutter=spec.get("draw_gutter", False))
        with Image.open(raw_path) as im:
            im.save(raw_path, format="PNG")
        rel = f"panels/{pid}.jpg"
        sha = _save_marked_panel(
            chap_dir / rel,
            w,
            h,
            border_pct=float(spec.get("border_pct", 0.0)),
            draw_gutter=spec.get("draw_gutter", False),
        )
        vcs_gap = int(spec.get("vcs_gap_before", 0))
        v_start = vcs_cursor + vcs_gap
        vcs_cursor = v_start + h
        items.append(
            PanelItem(
                panel_id=pid,
                sequence_number=idx,
                filename=f"{pid}.jpg",
                relative_path=rel,
                source_image=raw_name,
                crop_box=(0, 0, w, h),
                vcs_start=v_start,
                vcs_end=vcs_cursor,
                width=w,
                height=h,
                aspect_ratio=round(w / float(h), 4),
                sha256=sha,
            )
        )
    atomic_write_json(
        chap_dir / "manifest.json",
        PanelManifest(
            project_slug="dec4-proj",
            chapter_number="1",
            total_raw_images=len(items),
            total_panels=len(items),
            panels=items,
        ),
    )
    return chap_dir


def _four_sided_payload(
    *,
    panel_group: str = "group_1_story_art",
    group_conf: float = 0.96,
    structure_action: str = "ok_single",
    edges: tuple[int, int, int, int] = (10, 10, 10, 10),
    edge_conf: float = 0.92,
    edge_probs: Dict[str, Dict[str, float]] | None = None,
    text_tokens: int = 420,
    image_tokens: int = 555,
) -> Dict[str, Any]:
    l, t, r, b = edges
    answers: Dict[str, Any] = {
        "panel_group": {
            "type": "choice",
            "choice": panel_group,
            "confidence": group_conf,
            "probabilities": {panel_group: group_conf},
        },
        "structure_action": {
            "type": "choice",
            "choice": structure_action,
            "confidence": 0.93,
        },
    }
    for side, pct in zip(("left", "top", "right", "bottom"), (l, t, r, b)):
        custom_probs = (edge_probs or {}).get(side)
        if custom_probs is None:
            rem = max(0.0, 1.0 - edge_conf)
            custom_probs = {str(k): (edge_conf if k == pct else round(rem / 4.0, 6)) for k in CROP_PCT_OPTIONS}
        answers[f"crop_{side}"] = {
            "type": "choice",
            "choice": str(pct),
            "confidence": max(custom_probs.values()),
            "probabilities": custom_probs,
        }
    return {
        "answers": answers,
        "usage": {
            "total_input_tokens": text_tokens + image_tokens,
            "input_tokens_by_modality": [
                {"modality": "text", "tokens": text_tokens},
                {"modality": "image", "tokens": image_tokens},
            ],
            "total_output_tokens": 0,
        },
    }


def test_evaluate_4sided_crop_all_625_combinations_and_odd_dimensions():
    cfg = DecisionPolicyConfig(policy="4sided")
    for l, t, r, b in itertools.product(CROP_PCT_OPTIONS, repeat=4):
        answers = {
            f"crop_{side}": {"type": "choice", "choice": str(val), "confidence": 0.90, "probabilities": {str(val): 0.90, "0": 0.10}}
            for side, val in zip(("left", "top", "right", "bottom"), (l, t, r, b))
        }
        res = evaluate_4sided_crop(answers, 777, 1333, config=cfg)
        assert res["decision_valid"] is True
        assert res["crop_safe"] is True
        box = res["display_crop"]
        assert 0.0 <= box["x_min"] < box["x_max"] <= 1.0
        assert 0.0 <= box["y_min"] < box["y_max"] <= 1.0
        assert res["retained_width"] >= cfg.min_crop_width - 1e-6
        assert res["retained_height"] >= cfg.min_crop_height - 1e-6
        assert res["retained_area"] >= cfg.min_crop_area - 1e-6

    # All-40% (only 4% area) MUST trigger deterministic backoff cleanly
    all_40 = {
        f"crop_{side}": {"type": "choice", "choice": "40", "confidence": 0.95, "probabilities": {"40": 0.95, "0": 0.05}}
        for side in ("left", "top", "right", "bottom")
    }
    res_40 = evaluate_4sided_crop(all_40, 1000, 1000, config=cfg)
    assert res_40["guardrail_adjusted"] is True
    assert res_40["retained_area"] >= 0.50

    # Zero trims preserve full normalized Region and deterministic motion obeys 1.3 cutoff
    all_0 = {
        f"crop_{side}": {"type": "choice", "choice": "0", "confidence": 0.99, "probabilities": {"0": 0.99, "10": 0.01}}
        for side in ("left", "top", "right", "bottom")
    }
    res_0 = evaluate_4sided_crop(all_0, 777, 1333, config=cfg)
    assert res_0["display_crop"] == {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}
    assert compute_deterministic_motion(777, 1000, res_0["display_crop"]) == "static"
    assert compute_deterministic_motion(777, 1333, res_0["display_crop"]) == "scroll"


def test_4sided_confidence_backoff_nan_rejection_and_group_uncertainty(tmp_path, monkeypatch):
    cfg = DecisionPolicyConfig(policy="4sided", edge_conf_threshold=0.70, edge_margin_threshold=0.15, group_conf_threshold=0.55)

    # 1. Left has bimodal (0 vs 20) tie -> backs off to 0; Top splits across (20, 30) -> cumulative support keeps 20!
    answers = _four_sided_payload(
        edges=(20, 20, 10, 0),
        edge_probs={
            "left": {"0": 0.45, "10": 0.03, "20": 0.48, "30": 0.02, "40": 0.02},
            "top": {"0": 0.05, "10": 0.10, "20": 0.45, "30": 0.35, "40": 0.05},
            "right": {"0": 0.05, "10": 0.85, "20": 0.05, "30": 0.03, "40": 0.02},
            "bottom": {"0": 0.95, "10": 0.05, "20": 0.0, "30": 0.0, "40": 0.0},
        },
    )["answers"]
    crop_eval = evaluate_4sided_crop(answers, 1000, 1000, config=cfg)
    assert crop_eval["crop_edges_pct"] == {"left": 0, "top": 20, "right": 10, "bottom": 0}
    assert crop_eval["display_crop"] == {"x_min": 0.0, "y_min": 0.2, "x_max": 0.9, "y_max": 1.0}

    # 2. Invalid NaN probability rejected by DecisionClient
    chap_dir = _make_manifest_chapter(tmp_path, monkeypatch, [{"panel_id": "panel_001", "width": 600, "height": 800}])
    bad_client = DecisionClient(
        max_retries=1,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200,
                    content=b'{"answers":{"panel_group":{"choice":"group_1_story_art","probabilities":{"group_1_story_art":NaN}}}}',
                    headers={"content-type": "application/json"},
                )
            )
        ),
    )
    with pytest.raises(ProviderIntegrityError):
        run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=bad_client, config=cfg)

    # 3. Uncertain group_3_skip (conf=0.42 < 0.55) MUST NOT drop panel; routes to NEEDS_REVIEW safely
    uncertain_client = DecisionClient(
        max_retries=1,
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json=_four_sided_payload(panel_group="group_3_skip", group_conf=0.42))
            )
        ),
    )
    res = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=uncertain_client, config=cfg)
    assert res["needs_review_count"] == 1
    manifest = load_manifest(chap_dir)
    assert manifest.panels[0].status == PanelStatus.NEEDS_REVIEW
    assert manifest.panels[0].evidence_metadata["text_detection_uncertain"] is True
    assert manifest.panels[0].evidence_metadata["crop_safe"] is False


def test_content_blocked_isolates_single_panel_and_preserves_other_panels(tmp_path, monkeypatch):
    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 600, "height": 800},
            {"panel_id": "panel_002", "width": 600, "height": 800},
        ],
    )
    calls = 0

    def _handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": {"code": "image_safety", "message": "safety violation"}})
        return httpx.Response(200, json=_four_sided_payload(edges=(10, 0, 10, 0)))

    client = DecisionClient(max_retries=3, http_client=httpx.Client(transport=httpx.MockTransport(_handler)))
    with pytest.raises(ProviderContentBlockedError):
        client.call(
            image_path=chap_dir / "panels" / "panel_001.jpg",
            text_context="ctx",
            questions={"q": {"type": "choice", "instructions": "x", "criteria": {"a": "a", "b": "b"}}},
            api_key="key-123",
        )
    assert calls == 1  # fail fast without wasting retries

    calls = 0
    cfg = DecisionPolicyConfig(policy="4sided")
    summary = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client, config=cfg)
    assert calls == 2
    assert summary["needs_review_count"] == 1
    assert summary["counts_by_group"] == {"active": 1, "context_only": 0, "skip": 0}
    manifest = load_manifest(chap_dir)
    p1 = next(p for p in manifest.panels if p.panel_id == "panel_001")
    p2 = next(p for p in manifest.panels if p.panel_id == "panel_002")
    assert p1.status == PanelStatus.NEEDS_REVIEW
    assert p2.status == PanelStatus.ACTIVE
    assert p2.evidence_metadata["display_crop"] == {"x_min": 0.1, "y_min": 0.0, "x_max": 0.9, "y_max": 1.0}


def test_split_no_split_above_hw4_and_merge_verification_and_vcs_guards(tmp_path, monkeypatch):
    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 800, "height": 3600, "draw_gutter": True},  # H/W=4.5 >= 4.0
            {"panel_id": "panel_002", "width": 800, "height": 240},  # skip banner
            {"panel_id": "panel_003", "width": 800, "height": 600},
            {"panel_id": "panel_004", "width": 800, "height": 220},  # contiguous fragment of p3
        ],
    )
    cfg = DecisionPolicyConfig(policy="4sided")
    saw_no_split_on_hw45 = False
    saw_merge_verify = False

    def _handler(req: httpx.Request) -> httpx.Response:
        nonlocal saw_no_split_on_hw45, saw_merge_verify
        body = json.loads(req.content.decode("utf-8"))
        qkeys = set(body["questions"].keys())
        txt = next(item["text"] for item in body["input"] if item["type"] == "text")
        if "primary_cut" in qkeys:
            saw_no_split_on_hw45 = "no_split" in body["questions"]["primary_cut"]["criteria"]
            return httpx.Response(200, json={"answers": {"primary_cut": {"type": "choice", "choice": "no_split", "confidence": 0.91}}, "usage": {"total_input_tokens": 600}})
        if "merge_decision" in qkeys:
            saw_merge_verify = True
            return httpx.Response(200, json={"answers": {"merge_decision": {"type": "choice", "choice": "keep_separate", "confidence": 0.89}}, "usage": {"total_input_tokens": 500}})
        if "800x3600px" in txt:
            return httpx.Response(200, json=_four_sided_payload(structure_action="needs_split", edges=(0, 10, 0, 10)))
        if "800x240px" in txt:
            return httpx.Response(200, json=_four_sided_payload(panel_group="group_3_skip", structure_action="merge_with_prev"))
        if "800x220px" in txt:
            return httpx.Response(200, json=_four_sided_payload(structure_action="merge_with_prev", edges=(0, 0, 0, 0)))
        return httpx.Response(200, json=_four_sided_payload(edges=(10, 0, 10, 0)))

    client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_handler)))
    summary = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client, config=cfg)

    assert saw_no_split_on_hw45 is True
    assert saw_merge_verify is True
    assert summary["split_resolved"] == 0  # kept intact because model picked no_split
    assert summary["final_total_panels"] == 4  # merge blocked by keep_separate + skip guard
    assert summary["fast_path"] == "metadata_only"  # no raw re-extract when no split/merge applied!
    assert summary["total_input_tokens"] > 0 and "split_select" in summary["tokens_by_phase"] and "merge_verify" in summary["tokens_by_phase"]


def test_refresh_and_repack_recovery(tmp_path, monkeypatch):
    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [{"panel_id": "panel_001", "width": 600, "height": 800, "border_pct": 0.2}],
    )
    orig_sha = load_manifest(chap_dir).panels[0].sha256
    orig_bytes = (chap_dir / "panels" / "panel_001.jpg").read_bytes()

    calls = 0
    edge_trim = 20

    def _handler(_req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json=_four_sided_payload(
                edges=(edge_trim, 0, edge_trim, 10),
                edge_probs={
                    "left": {"0": 0.05, "10": 0.0, str(edge_trim): 0.95, "30": 0.0, "40": 0.0} if edge_trim == 20 else {"0": 0.05, "10": 0.95, "20": 0.0, "30": 0.0, "40": 0.0},
                    "top": {"0": 0.95, "10": 0.05, "20": 0.0, "30": 0.0, "40": 0.0},
                    "right": {"0": 0.05, "10": 0.0, str(edge_trim): 0.95, "30": 0.0, "40": 0.0} if edge_trim == 20 else {"0": 0.05, "10": 0.95, "20": 0.0, "30": 0.0, "40": 0.0},
                    "bottom": {"0": 0.05, "10": 0.95, "20": 0.0, "30": 0.0, "40": 0.0},
                },
            ),
        )

    client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_handler)))
    cfg = DecisionPolicyConfig(policy="4sided")
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client, config=cfg)
    assert calls == 1
    assert (chap_dir / "panels" / "panel_001.jpg").read_bytes() == orig_bytes
    assert load_manifest(chap_dir).panels[0].sha256 == orig_sha
    assert load_manifest(chap_dir).panels[0].evidence_metadata["crop_edges_pct"] == {"left": 20, "top": 0, "right": 20, "bottom": 10}

    # adaptive_tall_resolution preserves medium on regular panels and switches to high on tall panels
    from src.ingest.decision import _resolve_panel_resolution
    assert _resolve_panel_resolution(1080, 1400, cfg) == "medium"
    assert _resolve_panel_resolution(1080, 2600, cfg) == "high"

    # Second run without refresh hits cache cleanly with zero API calls
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client, config=cfg)
    assert calls == 1

    # Single explicit refresh=True bypasses cache cleanly
    edge_trim = 10
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, refresh=True, client=client, config=cfg)
    assert calls == 2
    assert load_manifest(chap_dir).panels[0].evidence_metadata["crop_edges_pct"] == {"left": 10, "top": 0, "right": 10, "bottom": 10}

    # Simulate interrupted backup recovery in apply_panel_plan
    (chap_dir / "panels").replace(chap_dir / ".repack_backup_panels")
    assert not (chap_dir / "panels").exists()
    plan = PanelPlan(
        project_slug="dec4-proj",
        chapter_number="1",
        items=[PanelPlanItem(op=PanelPlanOp.KEEP, source_panel_ids=["panel_001"], group=PanelStatus.ACTIVE)],
    )
    apply_panel_plan(chap_dir, plan)
    assert (chap_dir / "panels" / "panel_001.jpg").read_bytes() == orig_bytes


def test_static_visual_policy_honors_4sided_display_crop_and_rejects_unsafe(tmp_path, monkeypatch):
    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 600, "height": 800, "border_pct": 0.2},
            {"panel_id": "panel_002", "width": 400, "height": 1200, "border_pct": 0.1},
        ],
    )
    manifest = load_manifest(chap_dir)
    manifest.panels[0].evidence_metadata = {
        "text_detection_uncertain": False,
        "crop_safe": True,
        "decision_valid": True,
        "display_crop": {"x_min": 0.2, "y_min": 0.2, "x_max": 0.8, "y_max": 0.8},
    }
    manifest.panels[1].evidence_metadata = {
        "text_detection_uncertain": False,
        "crop_safe": True,
        "decision_valid": True,
        "display_crop": {"x_min": 0.1, "y_min": 0.0, "x_max": 0.9, "y_max": 1.0},
    }
    script = RecapScript(
        project_slug="dec4-proj",
        chapter_number="1",
        segments=[
            ScriptSegment(seq_id="001", evidence_panel_ids=["panel_001"], narrator_text="Static crop", duration=2.0),
            ScriptSegment(seq_id="002", evidence_panel_ids=["panel_002"], narrator_text="Tall static horizontal crop", duration=4.0),
        ],
    )

    # Static mode MUST apply display_crop to both normal panels and tall decomposed tiles
    vplan = generate_visual_plan(chap_dir, manifest, script, target_width=1280, target_height=720, motion_enabled=False)
    s0 = vplan.get_shots_for_segment("001")[0]
    assert s0.source_box == Region(x_min=0.2, y_min=0.2, x_max=0.8, y_max=0.8)
    with Image.open(chap_dir / "panels" / "panel_001.jpg") as im:
        frame = render_frame(im, s0, vplan, 0.0)
        cx, cy = frame.width // 2, frame.height // 2
        r, g, b = frame.getpixel((cx, cy))
        assert b > r + 80  # center retains inner blue subject box

    tall_shots = vplan.get_shots_for_segment("002")
    assert len(tall_shots) > 1
    assert all(math.isclose(s.source_box.x_min, 0.1) and math.isclose(s.source_box.x_max, 0.9) for s in tall_shots)

    # crop_safe=False MUST revert static render back to FULL Region() and block unsafe authored crop
    manifest.panels[0].evidence_metadata["crop_safe"] = False
    vplan_unsafe = generate_visual_plan(chap_dir, manifest, script, target_width=1280, target_height=720, motion_enabled=False)
    assert vplan_unsafe.get_shots_for_segment("001")[0].source_box == Region()
    tampered = vplan.model_copy(update={"shots": [s0.model_copy(update={"duration_sec": 2.0}) for _ in (0,)]})
    # Build complete tampered plan keeping 002 shots intact
    tampered_data = vplan_unsafe.model_dump(mode="json")
    tampered_data["shots"][0]["source_box"] = {"x_min": 0.2, "y_min": 0.2, "x_max": 0.8, "y_max": 0.8}
    with pytest.raises(ValueError, match="Unsafe crop"):
        validate_visual_plan(VisualPlan.model_validate(tampered_data), chap_dir, manifest, script, 1280, 720, 30, False)


def test_v7_panel_identity_after_repack_and_cache_integrity(tmp_path, monkeypatch):
    """V7 Fixes #1 & #2: split shifts panel_002 -> panel_003 without colliding on pass1[panel_003]; disk integrity + incomplete cache rejected."""
    from src.core.errors import DomainValidationError

    chap_dir = _make_manifest_chapter(
        tmp_path,
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 600, "height": 1800, "draw_gutter": True},  # Split -> panel_001, panel_002
            {"panel_id": "panel_002", "width": 600, "height": 800},   # Kept -> shifts to panel_003! (10,0,10,0)
            {"panel_id": "panel_003", "width": 600, "height": 900},   # Kept -> shifts to panel_004! SKIP (0,0,0,0)
        ],
    )
    sha_p2 = load_manifest(chap_dir).panels[1].sha256
    sha_p3 = load_manifest(chap_dir).panels[2].sha256

    api_calls = 0

    def _handler(req: httpx.Request) -> httpx.Response:
        nonlocal api_calls
        api_calls += 1
        body = json.loads(req.content.decode("utf-8"))
        qkeys = set(body["questions"].keys())
        txt = next(item["text"] for item in body["input"] if item["type"] == "text")
        if "primary_cut" in qkeys:
            return httpx.Response(200, json={"answers": {"primary_cut": {"choice": "cut_1", "confidence": 0.94}}, "usage": {"total_input_tokens": 100}})
        if "600x1800px" in txt:
            return httpx.Response(200, json=_four_sided_payload(structure_action="needs_split", edges=(0, 0, 0, 0)))
        if "600x800px" in txt:
            return httpx.Response(200, json=_four_sided_payload(panel_group="group_1_story_art", edges=(10, 0, 10, 0)))
        if "600x900px" in txt:
            return httpx.Response(200, json=_four_sided_payload(panel_group="group_3_skip", edges=(0, 0, 0, 0)))
        return httpx.Response(200, json=_four_sided_payload(panel_group="group_1_story_art", edges=(0, 20, 0, 20)))

    client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_handler)))
    summary = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client)
    assert summary["split_resolved"] == 1
    assert summary["final_total_panels"] == 4
    repacked = load_manifest(chap_dir)
    # Critical Fix #1 check: repacked panel_003 MUST hold raw panel_002's SHA and ACTIVE (10,0,10,0), NOT raw panel_003 SKIP!
    assert repacked.panels[2].panel_id == "panel_003"
    assert repacked.panels[2].sha256 == sha_p2
    assert repacked.panels[2].status == PanelStatus.ACTIVE
    assert repacked.panels[2].evidence_metadata["source_raw_panels"] == ["panel_002"]
    assert repacked.panels[2].evidence_metadata["crop_edges_pct"] == {"left": 10, "top": 0, "right": 10, "bottom": 0}
    assert repacked.panels[3].panel_id == "panel_004"
    assert repacked.panels[3].sha256 == sha_p3
    assert repacked.panels[3].status == PanelStatus.SKIP
    assert repacked.panels[3].evidence_metadata["source_raw_panels"] == ["panel_003"]

    # Immediate re-run uses verified SHA-bound cache with 0 API calls and keeps lineage exact
    calls_before_resume = api_calls
    summary2 = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client)
    assert api_calls == calls_before_resume
    assert summary2["cache_hits"]["pass1"] == 4

    # Fix #2a: Corrupt cache (incomplete raw_answers missing crop_bottom) is rejected as invalid cache hit
    cache = read_json(chap_dir / "decision_cache.json")
    cache["panel_003"]["raw_answers"].pop("crop_bottom", None)
    atomic_write_json(chap_dir / "decision_cache.json", cache)
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client)
    assert api_calls == calls_before_resume + 1  # re-queried panel_003 instead of trusting partial raw answers!

    # Fix #2b: Swapping panel image bytes on disk without updating manifest SHA fails integrity before cache/API
    Image.new("RGB", (600, 800), color=(1, 2, 3)).save(chap_dir / "panels" / "panel_003.jpg")
    with pytest.raises(DomainValidationError, match="checksum mismatch"):
        run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=client)


def test_s2_child_cache_isolation_merge_requires_verify_and_missing_confidence_backoff(tmp_path, monkeypatch):
    """Unconditional semantic merge verification; split children own independent Pass 2 cache; missing confidence backs off."""
    from src.core.errors import DomainValidationError
    from src.ingest.decision import _select_conservative_edge_trim, interpret_panel_answers

    raw_pct, acc_pct, conf, _, _ = _select_conservative_edge_trim(
        {"type": "choice", "choice": "30"},
        edge_conf_threshold=0.60,
        edge_margin_threshold=0.12,
        apply_guardrails=True,
    )
    assert raw_pct == 30 and acc_pct == 0 and conf is None

    no_conf_dec = interpret_panel_answers(
        {
            "answers": {
                "panel_group": {"type": "choice", "choice": "group_1_story_art"},
                "structure_action": {"type": "choice", "choice": "ok_single"},
                "crop_left": {"type": "choice", "choice": "20"},
                "crop_top": {"type": "choice", "choice": "0"},
                "crop_right": {"type": "choice", "choice": "0"},
                "crop_bottom": {"type": "choice", "choice": "0"},
            }
        },
        600,
        800,
        config=DecisionPolicyConfig(policy="4sided"),
    )
    assert no_conf_dec["status"] == PanelStatus.NEEDS_REVIEW.value
    assert no_conf_dec["review_reason"] == "missing_group_confidence"

    # VCS contiguity always invokes semantic merge check and refuses non-mergeable fragments
    chap_verify = _make_manifest_chapter(
        tmp_path / "nv",
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 800, "height": 600},
            {"panel_id": "panel_002", "width": 800, "height": 220},
        ],
    )
    verify_calls = 0

    def _nv_handler(r: httpx.Request) -> httpx.Response:
        nonlocal verify_calls
        body = json.loads(r.content)
        if "merge_decision" in body["questions"]:
            verify_calls += 1
            return httpx.Response(200, json={"answers": {"merge_decision": {"choice": "keep_separate", "confidence": 0.9}}, "usage": {"total_input_tokens": 100}})
        txt = next(i["text"] for i in body["input"] if i["type"] == "text")
        return httpx.Response(
            200,
            json=_four_sided_payload(
                structure_action="merge_with_prev" if "800x220px" in txt else "ok_single",
                edges=(0, 0, 0, 0),
            ),
        )

    nv_client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_nv_handler)))
    nv_summary = run_decision_pipeline(chap_verify, api_key="key-123", max_workers=1, client=nv_client)
    assert verify_calls == 1
    assert nv_summary["final_total_panels"] == 2
    assert nv_summary["merge_diagnostics"][0]["merged"] is False
    assert nv_summary["merge_diagnostics"][0]["action"] == "keep_separate"

    # Split children store OWN Pass 2 raw_answers & __pass2_* cache with source lineage
    chap_dir = _make_manifest_chapter(
        tmp_path / "split",
        monkeypatch,
        [
            {"panel_id": "panel_001", "width": 800, "height": 2400, "draw_gutter": True},
        ],
    )
    pass2_seen = 0

    def _split_handler(req: httpx.Request) -> httpx.Response:
        nonlocal pass2_seen
        body = json.loads(req.content.decode("utf-8"))
        qkeys = set(body["questions"].keys())
        txt = next(item["text"] for item in body["input"] if item["type"] == "text")
        if "primary_cut" in qkeys:
            return httpx.Response(200, json={"answers": {"primary_cut": {"choice": "cut_1", "confidence": 0.94}}, "usage": {"total_input_tokens": 200}})
        if "800x2400px" in txt:
            return httpx.Response(200, json=_four_sided_payload(structure_action="needs_split", edges=(0, 0, 0, 0)))
        pass2_seen += 1
        edges = (10, 0, 10, 0) if pass2_seen == 1 else (0, 20, 0, 20)
        return httpx.Response(200, json=_four_sided_payload(edges=edges, edge_conf=0.80))

    sp_client = DecisionClient(http_client=httpx.Client(transport=httpx.MockTransport(_split_handler)))
    summary = run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=sp_client)
    assert summary["split_resolved"] == 1 and summary["pass2_reclassified"] == 2
    cache = read_json(chap_dir / "decision_cache.json")
    assert cache["panel_001"]["panel_origin"] == "child_pass2"
    assert cache["panel_002"]["panel_origin"] == "child_pass2"
    assert cache["__pass2_panel_001"]["source_raw_panels"] == ["panel_001"]
    assert cache["panel_001"]["raw_answers"]["structure_action"]["choice"] == "ok_single"

    # Re-running cached chapter hits verified child_pass2 entries with ZERO API calls
    run_decision_pipeline(chap_dir, api_key="key-123", max_workers=1, client=sp_client)
    assert pass2_seen == 2

    # Resolution override on DecisionPolicyConfig rejected (B08)
    with pytest.raises(ValueError, match="fixed at medium / high"):
        DecisionPolicyConfig(policy="4sided", image_resolution="high")
