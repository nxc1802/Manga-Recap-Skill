"""Deterministic evidence-to-VisualPlan compiler shared by all backends."""
from __future__ import annotations
import math
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image
from src.core.schemas import PanelStatus
from src.core.visual_contract import Region, Shot, VisualPlan, VisualRole, ProductionMotion
from src.core.storage import load_manifest, load_recap_script, calculate_sha256

TALL_PANEL_THRESHOLD = 2.4


def is_tall_panel(width, height, threshold_h_over_w=TALL_PANEL_THRESHOLD):
    if width <= 0 or height <= 0:
        raise ValueError("Invalid panel dimensions")
    return height / width > threshold_h_over_w


def decompose_tall_panel(panel_id, width, height, **_):
    if not is_tall_panel(width, height):
        return [Region()]
    count = max(2, math.ceil(height / (width * 1.1)))
    span = width * 1.4 / height
    return [Region(y_min=i * (1 - span) / (count - 1),
                   y_max=min(1.0, i * (1 - span) / (count - 1) + span))
            for i in range(count)]


def evidence_path(chap_dir, panel):
    root = Path(chap_dir).resolve()
    path = (root / panel.relative_path).resolve()
    if not path.is_relative_to(root / "panels") or not path.is_file():
        raise ValueError(f"Missing or unsafe panel evidence: {panel.panel_id}")
    if calculate_sha256(path) != panel.sha256:
        raise ValueError(f"Panel evidence hash mismatch: {panel.panel_id}")
    with Image.open(path) as image:
        image.load()
        if image.size != (panel.width, panel.height):
            raise ValueError(f"Panel dimensions mismatch: {panel.panel_id}")
    return path


def detect_omit_visual_band(img_path_or_crop=None, panel_item=None, min_height_px=80, variance_threshold=100):
    # Text classification must be explicit agent evidence. Pixel heuristics cannot
    # distinguish sparse artwork from text with adequate confidence.
    if panel_item is not None:
        if panel_item.status == PanelStatus.NEEDS_REVIEW:
            raise ValueError(f"Panel needs human review: {panel_item.panel_id}")
        if panel_item.status in {PanelStatus.SKIP, PanelStatus.CONTEXT_ONLY}:
            return True, panel_item.notes or "source_not_for_display"
        if panel_item.notes and "text_only" in panel_item.notes.lower():
            return True, "text_only_agent_evidence"
    if panel_item is not None and panel_item.height < min_height_px:
        return True, "short_sliver"
    if img_path_or_crop is not None:
        if isinstance(img_path_or_crop, Image.Image):
            pixels = np.asarray(img_path_or_crop.convert("L"), dtype=float)
        else:
            with Image.open(img_path_or_crop) as image:
                pixels = np.asarray(image.convert("L"), dtype=float)
        if pixels.shape[0] < min_height_px:
            return True, "short_sliver"
        if float(pixels.var()) < variance_threshold:
            return True, "blank_band"
    return False, None


def validate_narration_evidence(chapter_dir, script, manifest=None):
    manifest = manifest if manifest is not None else load_manifest(chapter_dir)
    if script is None or manifest is None or not script.segments or not manifest.panels:
        raise ValueError("Narration and real panel evidence are required")
    if script.project_slug != manifest.project_slug or script.chapter_number != manifest.chapter_number:
        raise ValueError("Narration/evidence identity mismatch")
    known = set()
    for panel in manifest.panels:
        evidence_path(chapter_dir, panel)
        if panel.status == PanelStatus.NEEDS_REVIEW:
            raise ValueError(f"Panel needs review: {panel.panel_id}")
        known.update((panel.panel_id, panel.filename, Path(panel.filename).stem))
    for segment in script.segments:
        ProductionMotion(segment.camera_motion.value)
        if segment.panel_ids != segment.evidence_panel_ids:
            raise ValueError("Narration evidence aliases conflict")
        if segment.status == "needs_review" or "[CAN_XAC_MINH" in segment.narrator_text:
            raise ValueError(f"Unresolved narration: {segment.seq_id}")
        if not segment.evidence_panel_ids or any(pid not in known for pid in segment.evidence_panel_ids):
            raise ValueError(f"Narration lacks panel evidence: {segment.seq_id}")
    return manifest


def generate_visual_plan(chap_dir, manifest=None, script=None, target_width=1920,
                         target_height=1080, fps=30, motion_enabled=None,
                         max_panel_aspect_h_over_w=TALL_PANEL_THRESHOLD):
    if max_panel_aspect_h_over_w != TALL_PANEL_THRESHOLD:
        raise ValueError("Production tall-panel threshold is frozen at H/W > 2.4")
    root = Path(chap_dir)
    manifest = manifest if manifest is not None else load_manifest(root)
    script = script if script is not None else load_recap_script(root)
    validate_narration_evidence(root, script, manifest)
    if str(script.chapter_number) != str(manifest.chapter_number):
        raise ValueError("Script/manifest chapter mismatch")
    if script.project_slug != manifest.project_slug:
        raise ValueError("Script/manifest project mismatch")
    by_id, approved, omitted = {}, [], {}
    for panel in manifest.panels:
        for key in {panel.panel_id, panel.filename, Path(panel.filename).stem}:
            if key in by_id and by_id[key] != panel:
                raise ValueError(f"Ambiguous panel identifier: {key}")
            by_id[key] = panel
        path = evidence_path(root, panel)
        omit, reason = detect_omit_visual_band(path, panel)
        omitted[panel.panel_id] = reason if omit else None
        if not omit and panel.status == PanelStatus.ACTIVE:
            approved.append(panel)
    approved_ids = {p.panel_id for p in approved}
    panels_order = {p.panel_id: i for i, p in enumerate(manifest.panels)}
    evidence_order = []
    resolved = []
    for segment in script.segments:
        if segment.status == "needs_review" or "[CAN_XAC_MINH" in segment.narrator_text:
            raise ValueError(f"Unresolved narration: {segment.seq_id}")
        if not segment.duration or not math.isfinite(segment.duration) or segment.duration <= 0:
            raise ValueError(f"Measured audio duration required: {segment.seq_id}")
        if not segment.evidence_panel_ids:
            raise ValueError(f"Narration has no evidence: {segment.seq_id}")
        try:
            panels = [by_id[pid] for pid in segment.evidence_panel_ids]
        except KeyError as exc:
            raise ValueError(f"Unknown evidence panel: {exc}") from exc
        resolved.append((segment, panels))
        evidence_order.extend(panels)
    shots = []
    previous_approved = None
    for segment, panels in resolved:
        if segment.shots:
            raise ValueError("Legacy embedded shots must be migrated to canonical VisualPlan")
        # No aliasing of legacy pan, zoom, subject pan or jitter is permitted.
        motion = ProductionMotion(segment.camera_motion.value)
        if motion_enabled is False and motion == ProductionMotion.SLOW_PAN_DOWN:
            raise ValueError("Motion conflicts with explicitly static setup")
        candidates = []
        for panel in panels:
            reason = omitted[panel.panel_id]
            substitute = None
            substitution_reason = None
            if reason:
                same_segment = [p for p in panels if p.panel_id in approved_ids]
                adjacent = [p for p in evidence_order if p.panel_id in approved_ids
                            and abs(panels_order[p.panel_id] - panels_order[panel.panel_id]) == 1]
                same_page = [p for p in approved if p.source_image == panel.source_image]
                if same_segment:
                    substitute, substitution_reason = same_segment[0], "same_segment"
                elif adjacent:
                    substitute, substitution_reason = adjacent[0], "adjacent_evidence"
                elif same_page:
                    substitute, substitution_reason = same_page[0], "same_source_page"
                elif previous_approved:
                    substitute, substitution_reason = previous_approved, "previous_approved"
                else:
                    raise ValueError(f"No evidenced visual substitute: {panel.panel_id}")
            displayed = substitute or panel
            metadata = displayed.evidence_metadata
            if not isinstance(metadata.get("text_detection_uncertain", True), bool):
                raise ValueError("text_detection_uncertain must be an explicit boolean")
            focus = [Region.model_validate(x) for x in metadata.get("focus_regions", [])]
            avoid = [Region.model_validate(x) for x in metadata.get("avoid_regions", [])]
            # Uncertain text detection and absent safe focus evidence use full panel.
            uncertain = metadata.get("text_detection_uncertain", True) or bool(avoid and not focus)
            if uncertain or substitute:
                boxes, used_motion = [Region()], ProductionMotion.STATIC
            elif avoid:
                safe = [f for f in focus if not any(_intersects(f, a) for a in avoid)]
                boxes, used_motion = (safe[:1], ProductionMotion.STATIC_FOCUS) if safe else ([Region()], ProductionMotion.STATIC)
            elif is_tall_panel(panel.width, panel.height):
                boxes, used_motion = decompose_tall_panel(panel.panel_id, panel.width, panel.height), ProductionMotion.STATIC
            else:
                box = Region.model_validate(segment.focus_box.model_dump()) if segment.focus_box else Region()
                boxes, used_motion = [box], motion
            for box in boxes:
                candidates.append(dict(source_panel_id=panel.panel_id,
                    substitute_panel_id=substitute.panel_id if substitute else None,
                    source_box=box, focus_regions=focus, avoid_regions=avoid,
                    visual_role=VisualRole.OMIT_VISUAL if reason else VisualRole.SHOW,
                    visual_omit_reason=reason, substitution_reason=substitution_reason,
                    motion_type=used_motion,
                    scroll_fraction_per_second=0.12 if used_motion == ProductionMotion.SLOW_PAN_DOWN else 0))
            previous_approved = displayed
        duration = segment.duration / len(candidates)
        for item in candidates:
            shots.append(Shot(shot_id=f"shot_{len(shots)+1:05d}", segment_id=segment.seq_id,
                              duration_sec=duration, **item))
    inputs = dict(script=script.model_dump(mode="json"), manifest=manifest.model_dump(mode="json"),
                  width=target_width, height=target_height, fps=fps, motion_enabled=motion_enabled)
    input_fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return VisualPlan(chapter_number=manifest.chapter_number, input_fingerprint=input_fingerprint, motion_enabled=motion_enabled, shots=shots,
                      fps=fps, target_width=target_width, target_height=target_height,
                      total_duration_sec=sum(s.duration for s in script.segments))


def _intersects(a, b):
    return a.x_min < b.x_max and a.x_max > b.x_min and a.y_min < b.y_max and a.y_max > b.y_min


def validate_visual_plan(plan, chapter_dir, manifest, script, target_width, target_height, fps, motion_enabled):
    """Validate authored N:M shots independently of narration evidence coverage.

    An evidence panel need not appear in the visual sequence. Each rendered shot
    must still be supported by its segment, safe source metadata, and timeline.
    """
    plan = VisualPlan.model_validate(plan.model_dump(mode="json"))
    baseline = generate_visual_plan(chapter_dir, manifest, script, target_width=target_width,
        target_height=target_height, fps=fps, motion_enabled=motion_enabled)
    for field in ("input_fingerprint", "motion_enabled", "chapter_number", "target_width", "target_height", "fps", "total_duration_sec"):
        if getattr(plan, field) != getattr(baseline, field):
            raise ValueError(f"VisualPlan source/config is stale: {field}")
    sequence = list(dict.fromkeys(s.segment_id for s in plan.shots))
    if sequence != [s.seq_id for s in script.segments]:
        raise ValueError("VisualPlan segment sequence differs from script")
    for segment in script.segments:
        expected_shots = baseline.get_shots_for_segment(segment.seq_id)
        by_panel = {}
        for reference in expected_shots:
            by_panel.setdefault(reference.source_panel_id, []).append(reference)
        last_tall_box = {}
        shots = plan.get_shots_for_segment(segment.seq_id)
        if not math.isclose(sum(s.duration_sec for s in shots), segment.duration, abs_tol=1e-6):
            raise ValueError("Segment duration differs from measured narration")
        for shot in shots:
            if shot.source_panel_id not in by_panel:
                raise ValueError("Rendered source lacks segment evidence")
            references = by_panel[shot.source_panel_id]
            reference = references[0]
            displayed_id = shot.substitute_panel_id or shot.source_panel_id
            panel = next(p for p in manifest.panels if p.panel_id == displayed_id)
            if is_tall_panel(panel.width, panel.height):
                # An authored hold may retain the whole panel. Crops must be
                # evidenced canonical regions (including approved safe focus).
                if shot.source_box == Region():
                    if shot.motion_type != ProductionMotion.STATIC:
                        raise ValueError("Tall full-panel hold must remain static")
                elif not any(shot.source_box == ref.source_box for ref in references):
                    raise ValueError("Tall crop is not a canonical safe region")
                previous_box = last_tall_box.get(displayed_id)
                if previous_box is not None and (shot.source_box.y_min < previous_box.y_min - 1e-9
                        or shot.source_box.y_max < previous_box.y_max - 1e-9):
                    raise ValueError("Tall shot sequence reverses vertical order")
                last_tall_box[displayed_id] = shot.source_box
            for field in ("visual_role", "visual_omit_reason", "substitute_panel_id", "substitution_reason", "focus_regions", "avoid_regions"):
                if getattr(shot, field) != getattr(reference, field):
                    raise ValueError(f"VisualPlan changes evidenced visual classification: {field}")
            if motion_enabled is False and shot.motion_type == ProductionMotion.SLOW_PAN_DOWN:
                raise ValueError("Motion conflicts with static series setup")
            # A reference full static frame is a conservative safety decision.
            # An authored plan cannot use uncertain text evidence to crop harder.
            if reference.source_box == Region() and reference.motion_type == ProductionMotion.STATIC:
                panel = next(p for p in manifest.panels if p.panel_id == (shot.substitute_panel_id or shot.source_panel_id))
                uncertain = panel.evidence_metadata.get("text_detection_uncertain", True)
                if uncertain or shot.substitute_panel_id or (shot.avoid_regions and reference.source_box == Region()):
                    if shot.source_box != Region() or shot.motion_type != ProductionMotion.STATIC:
                        raise ValueError("Unsafe crop/motion for uncertain text or substitute")
            if shot.source_box != Region() and any(_intersects(shot.source_box, region) for region in shot.avoid_regions):
                raise ValueError("Authored crop intersects avoid region")
            if shot.avoid_regions and shot.source_box == Region() and shot.motion_type != ProductionMotion.STATIC:
                raise ValueError("Full-panel text fallback must remain static")
        for previous, current in zip(shots, shots[1:]):
            if previous.source_panel_id == current.source_panel_id and previous.motion_type == ProductionMotion.SLOW_PAN_DOWN:
                panel = next(p for p in manifest.panels if p.panel_id == previous.source_panel_id)
                box = previous.source_box
                visible = min(box.y_max - box.y_min,
                    (box.x_max - box.x_min) * panel.width * plan.target_height / (plan.target_width * panel.height))
                travel = min(box.y_max - box.y_min - visible, previous.scroll_fraction_per_second * previous.duration_sec)
                if current.source_box.y_min < box.y_min + travel - 1e-9:
                    raise ValueError("Authored shot sequence reverses vertical motion")
    return plan


def resolve_visual_plan(chapter_dir, manifest, script, **settings):
    path = Path(chapter_dir) / "visual_plan.json"
    if not path.exists():
        return generate_visual_plan(chapter_dir, manifest, script, **settings)
    return validate_visual_plan(VisualPlan.load(path), chapter_dir, manifest, script, **settings)
