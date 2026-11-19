"""
src/core/decision_engine.py
Per-panel classification, split/merge routing, 2D center crop selection, and motion
decisions using Google Gemini Decisions API (`gemini-decision-1`, 0 output tokens).
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from PIL import Image

from src.core.contact_sheet import ContactSheetGenerator
from src.core.logger import logger
from src.core.panel_repack import (
    PanelPlan,
    PanelPlanItem,
    PanelPlanOp,
    apply_panel_plan,
    inspect_panels_with_candidates,
)
from src.core.schemas import PanelManifest, PanelStatus, utc_iso_now
from src.core.storage import atomic_write_json, load_manifest

DECISIONS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
DECISIONS_MODEL = "gemini-decision-1"

# 9 2D center-crop presets (applied ONLY to Group 1 ACTIVE panels during render)
CENTER_CROP_2D_PRESETS: Dict[str, Dict[str, float]] = {
    "full": {"x_min": 0.00, "y_min": 0.00, "x_max": 1.00, "y_max": 1.00},
    "center_v80": {"x_min": 0.00, "y_min": 0.10, "x_max": 1.00, "y_max": 0.90},
    "center_2d_tight": {"x_min": 0.08, "y_min": 0.10, "x_max": 0.92, "y_max": 0.90},
    "top_focus": {"x_min": 0.04, "y_min": 0.00, "x_max": 0.96, "y_max": 0.80},
    "bottom_focus": {"x_min": 0.04, "y_min": 0.20, "x_max": 0.96, "y_max": 1.00},
    "left_focus_2d": {"x_min": 0.00, "y_min": 0.08, "x_max": 0.82, "y_max": 0.92},
    "right_focus_2d": {"x_min": 0.18, "y_min": 0.08, "x_max": 1.00, "y_max": 0.92},
    "top_left_2d": {"x_min": 0.00, "y_min": 0.00, "x_max": 0.84, "y_max": 0.82},
    "top_right_2d": {"x_min": 0.16, "y_min": 0.00, "x_max": 1.00, "y_max": 0.82},
}


def _resolve_api_key(explicit_key: Optional[str] = None) -> str:
    load_dotenv()
    key = explicit_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "Missing GEMINI_API_KEY in environment or .env for gemini-decision-1."
        )
    return key.strip()


def _encode_image_b64(image_path: Path, max_height: int = 1024) -> str:
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        if h > max_height:
            scale = max_height / float(h)
            rgb = rgb.resize(
                (max(1, int(round(w * scale))), max_height),
                Image.Resampling.LANCZOS,
            )
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def call_gemini_decision(
    *,
    image_path: Path,
    text_context: str,
    questions: Dict[str, Dict[str, Any]],
    api_key: str,
    max_height: int = 1024,
    timeout: int = 30,
    max_retries: int = 6,
) -> Dict[str, Any]:
    """Calls Google Gemini Decisions API (`gemini-decision-1`) with 429 rate-limit retry."""
    b64 = _encode_image_b64(image_path, max_height=max_height)
    payload = {
        "model": DECISIONS_MODEL,
        "input": [
            {"type": "image", "data": b64, "mime_type": "image/jpeg"},
            {"type": "text", "text": text_context},
        ],
        "questions": questions,
    }
    for attempt in range(max_retries):
        resp = requests.post(
            DECISIONS_ENDPOINT,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 and attempt < max_retries - 1:
            wait_sec = 25 + attempt * 10
            logger.warning(f"Decision API 429 rate limit; sleeping {wait_sec}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait_sec)
            continue
        raise RuntimeError(f"Gemini Decisions API error ({resp.status_code}): {resp.text[:300]}")
    raise RuntimeError("Gemini Decisions API exhausted retries on 429.")


def _build_panel_questions(width: int, height: int) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    hw = round(height / float(max(1, width)), 2)
    ctx = (
        f"Webtoon panel metadata: width={width}px, height={height}px, height/width ratio={hw:.2f}. "
        "Classify this single panel independently for manga recap video production."
    )
    questions: Dict[str, Dict[str, Any]] = {
        "panel_group": {
            "type": "choice",
            "instructions": "Classify this panel into one of the three production groups.",
            "criteria": {
                "group_1_story_art": (
                    "Contains manga/webtoon artwork showing characters, creatures, action, magic effects, "
                    "or environments (with or without speech bubbles)."
                ),
                "group_2_text_only": (
                    "Contains ONLY speech bubbles, narration text, sound effects (SFX), or a chapter title card "
                    "on a plain solid black, white, or blank gradient background with NO character or scene drawing."
                ),
                "group_3_skip": (
                    "Website advertisement banner (e.g. TRUYENQQQ.COM promo), standalone translator watermark/logo "
                    "(e.g. RINKO COMICS.COM), dotted separator, or blank transition strip with no story dialogue."
                ),
            },
        },
        "structure_action": {
            "type": "choice",
            "instructions": "Check if this panel has a structural segmentation defect.",
            "criteria": {
                "ok_single": (
                    "Complete single frame, OR a single continuous tall illustration/scene without separate "
                    "stacked panel borders, OR a non-story text/ad card."
                ),
                "needs_split": (
                    "Very tall composite strip (especially H/W > 2.6) containing TWO OR MORE distinct sequential "
                    "story frames/scenes stacked vertically (including frames bridged by a speech bubble in between)."
                ),
                "merge_with_prev": (
                    "Very short cut-off sliver or half-sliced fragment (H/W < 0.4) clearly cut mid-speech-bubble "
                    "or mid-face that belongs together with the immediately preceding panel."
                ),
            },
        },
        "center_crop_2d": {
            "type": "choice",
            "instructions": (
                "Choose the best 2D display framing crop for a 16:9 video frame focusing on character faces "
                "and visual action while avoiding edge speech bubbles and dead margins."
            ),
            "criteria": {
                "full": "Keep 100% of image (square/landscape H/W <= 1.25 or tight composition touching all edges).",
                "center_v80": "Keep middle 80% vertically (x:0..1, y:0.10..0.90), trimming top/bottom empty margin or edge bubbles.",
                "center_2d_tight": "Keep 2D center subject box (x:0.08..0.92, y:0.10..0.90), trimming bubbles/margins on all 4 sides.",
                "top_focus": "Focus upper 80% (x:0.04..0.96, y:0.00..0.80) when key character/face is near the top.",
                "bottom_focus": "Focus lower 80% (x:0.04..0.96, y:0.20..1.00) when key character/face is near the bottom.",
                "left_focus_2d": "Focus left-center 2D (x:0.00..0.82, y:0.08..0.92) when subject is on left and bubble/blank on right.",
                "right_focus_2d": "Focus right-center 2D (x:0.18..1.00, y:0.08..0.92) when subject is on right and bubble/blank on left.",
                "top_left_2d": "Focus top-left 2D (x:0.00..0.84, y:0.00..0.82) for upper-left character framing.",
                "top_right_2d": "Focus top-right 2D (x:0.16..1.00, y:0.00..0.82) for upper-right character framing.",
            },
        },
        "motion_mode": {
            "type": "choice",
            "instructions": "Select video camera motion for this panel based on its aspect ratio and layout.",
            "criteria": {
                "static": "Keep static with side padding (square, landscape, or standard portrait panel with H/W <= 1.85).",
                "slow_pan_down": "Smooth downward scroll (only for very tall vertical strips with H/W > 1.85 whose width would look too narrow if fit statically).",
            },
        },
    }
    return ctx, questions


def decide_single_panel(
    image_path: Path,
    width: int,
    height: int,
    api_key: str,
) -> Dict[str, Any]:
    """Runs standardized 4-question Gemini Decision 1 evaluation for a single panel image."""
    ctx, questions = _build_panel_questions(width, height)
    res = call_gemini_decision(
        image_path=image_path,
        text_context=ctx,
        questions=questions,
        api_key=api_key,
    )
    answers = res.get("answers", {})
    group_key = answers.get("panel_group", {}).get("choice", "group_1_story_art")
    struct_key = answers.get("structure_action", {}).get("choice", "ok_single")
    crop_key = answers.get("center_crop_2d", {}).get("choice", "full")
    motion_key = answers.get("motion_mode", {}).get("choice", "static")

    group_map = {
        "group_1_story_art": PanelStatus.ACTIVE,
        "group_2_text_only": PanelStatus.CONTEXT_ONLY,
        "group_3_skip": PanelStatus.SKIP,
    }
    status = group_map.get(group_key, PanelStatus.ACTIVE)

    # Disable crop & motion when panel is Group 2 (text-only) or Group 3 (skip)
    if status != PanelStatus.ACTIVE:
        crop_key = "full"
        motion_key = "static"

    hw = height / float(max(1, width))
    # Only allow needs_split on genuinely tall story panels; prevent accidental splitting of short panels
    if struct_key == "needs_split" and (status != PanelStatus.ACTIVE or hw < 2.2):
        struct_key = "ok_single"

    return {
        "panel_group": group_key,
        "status": status.value,
        "structure_action": struct_key,
        "center_crop_2d": crop_key,
        "display_crop": CENTER_CROP_2D_PRESETS.get(crop_key, CENTER_CROP_2D_PRESETS["full"]),
        "motion_mode": "scroll" if motion_key == "slow_pan_down" else "static",
        "usage": res.get("usage", {}),
    }


def _choose_cut_ratios_via_decision(
    chap_dir: Path,
    panel_id: str,
    hw_ratio: float,
    api_key: str,
) -> Optional[List[float]]:
    """Draws numbered cut lines on a tall multi-frame panel and asks Gemini Decision 1 to pick cut(s)."""
    insp = inspect_panels_with_candidates(chap_dir, [panel_id], max_candidates=8)["inspected"][0]
    cands = insp["candidates"]
    if not cands:
        return None
    inspect_path = chap_dir / insp["inspect_image"]

    criteria: Dict[str, str] = {}
    for c in cands:
        criteria[f"cut_{c['index']}"] = (
            f"Split cleanly at horizontal red line CUT #{c['index']} (y_ratio={c['ratio']:.3f}) "
            "inside a white/empty transition gap between two stacked comic frames without cutting through faces."
        )
    if hw_ratio < 4.0:
        criteria["no_split"] = "Keep intact only if every red cut line clearly slices straight through a face or main body."

    questions: Dict[str, Dict[str, Any]] = {
        "primary_cut": {
            "type": "choice",
            "instructions": (
                "Inspect the horizontal red lines labeled CUT #1..#N. Pick the ONE cut line that lies "
                "in a blank/transition gap between two distinct frames."
            ),
            "criteria": criteria,
        }
    }
    if hw_ratio >= 5.2 and len(cands) >= 3:
        sec_criteria = {"none": "No second split needed; one split is sufficient."}
        for c in cands:
            sec_criteria[f"cut_{c['index']}"] = f"Also split at CUT #{c['index']} (y_ratio={c['ratio']:.3f}) to separate a 3rd distinct frame."
        questions["secondary_cut"] = {
            "type": "choice",
            "instructions": "Because this strip is very tall, pick a second cut line in another gap if there are 3 distinct frames, else choose none.",
            "criteria": sec_criteria,
        }

    res = call_gemini_decision(
        image_path=inspect_path,
        text_context=f"Annotated tall panel {panel_id} (H/W={hw_ratio:.2f}) with candidate cut lines.",
        questions=questions,
        api_key=api_key,
        max_height=2048,
    )
    answers = res.get("answers", {})
    p_choice = answers.get("primary_cut", {}).get("choice", "no_split")
    if p_choice == "no_split":
        return None

    cand_map = {f"cut_{c['index']}": c["ratio"] for c in cands}
    chosen: List[float] = []
    if p_choice in cand_map:
        chosen.append(cand_map[p_choice])
    s_choice = answers.get("secondary_cut", {}).get("choice", "none")
    if s_choice in cand_map and cand_map[s_choice] not in chosen:
        if all(abs(cand_map[s_choice] - r) >= 0.12 for r in chosen):
            chosen.append(cand_map[s_choice])
    chosen.sort()
    return chosen or None


def run_decision_pipeline(
    chapter_dir: Path,
    *,
    api_key: Optional[str] = None,
    max_workers: int = 3,
    reclassify_all: bool = False,
) -> Dict[str, Any]:
    """
    2-Pass non-duplicative Decision API pipeline:
      Pass 1: Classify every raw panel independently (group 1/2/3, split/merge, 2D center crop, motion).
      Split/Merge: Resolve only `needs_split` / `merge_with_prev` panels via VCS repack.
      Pass 2: Re-classify ONLY newly created split/merged child panels (0 duplicate calls on unchanged panels).
    """
    chap_dir = Path(chapter_dir)
    manifest = load_manifest(chap_dir)
    if manifest is None or not manifest.panels:
        raise ValueError(f"Missing or empty manifest.json in {chap_dir}; run 'segment' first.")

    key = _resolve_api_key(api_key)
    panels = manifest.panels
    cache_path = chap_dir / "decision_cache.json"
    pass1: Dict[str, Dict[str, Any]] = {}
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            for p in panels:
                if p.panel_id in cached and cached[p.panel_id].get("sha256") == p.sha256:
                    pass1[p.panel_id] = cached[p.panel_id]["decision"]
        except Exception:
            pass1 = {}
    total_input_tokens = 0

    pending = [p for p in panels if p.panel_id not in pass1]
    logger.info(
        f"DecisionEngine Pass 1: {len(pending)} pending / {len(panels)} total panels via {DECISIONS_MODEL}..."
    )
    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(decide_single_panel, chap_dir / p.relative_path, p.width, p.height, key): p
                for p in pending
            }
            for fut in as_completed(futures):
                p = futures[fut]
                dec = fut.result()
                pass1[p.panel_id] = dec
                total_input_tokens += int(dec.get("usage", {}).get("total_input_tokens", 0))
        cache_payload = {
            p.panel_id: {"sha256": p.sha256, "decision": pass1[p.panel_id]}
            for p in panels
            if p.panel_id in pass1
        }
        atomic_write_json(cache_path, cache_payload)

    # Build PanelPlan resolving needs_split and merge_with_prev
    split_targets = [p for p in panels if pass1[p.panel_id]["structure_action"] == "needs_split"]
    split_cuts: Dict[str, List[float]] = {}
    if split_targets:
        logger.info(f"DecisionEngine: Resolving cut positions for {len(split_targets)} multi-frame panels...")
        with ThreadPoolExecutor(max_workers=min(max_workers, len(split_targets))) as pool:
            futs = {
                pool.submit(_choose_cut_ratios_via_decision, chap_dir, p.panel_id, p.height / float(max(1, p.width)), key): p
                for p in split_targets
            }
            for fut in as_completed(futs):
                p = futs[fut]
                ratios = fut.result()
                if ratios:
                    split_cuts[p.panel_id] = ratios

    plan_items: List[PanelPlanItem] = []
    idx = 0
    while idx < len(panels):
        p = panels[idx]
        dec = pass1[p.panel_id]
        status = PanelStatus(dec["status"])

        # Handle merge_with_prev by absorbing current fragment into previous keep item if contiguous
        if dec["structure_action"] == "merge_with_prev" and plan_items and plan_items[-1].op in (PanelPlanOp.KEEP, PanelPlanOp.MERGE):
            prev = plan_items[-1]
            plan_items[-1] = PanelPlanItem(
                op=PanelPlanOp.MERGE,
                source_panel_ids=prev.source_panel_ids + [p.panel_id],
                group=prev.group,
                reason="decision_merge_fragment",
            )
            idx += 1
            continue

        if p.panel_id in split_cuts:
            plan_items.append(
                PanelPlanItem(
                    op=PanelPlanOp.SPLIT_AT,
                    source_panel_ids=[p.panel_id],
                    group=PanelStatus.ACTIVE,
                    cut_ratios=split_cuts[p.panel_id],
                    reason="decision_split_multi_frame",
                )
            )
        elif status == PanelStatus.SKIP:
            plan_items.append(
                PanelPlanItem(
                    op=PanelPlanOp.DROP,
                    source_panel_ids=[p.panel_id],
                    group=PanelStatus.SKIP,
                    reason="decision_group_3_skip",
                )
            )
        else:
            plan_items.append(
                PanelPlanItem(
                    op=PanelPlanOp.KEEP,
                    source_panel_ids=[p.panel_id],
                    group=status,
                    tall_motion_hint=dec["motion_mode"] if status == PanelStatus.ACTIVE else None,
                    reason=f"decision_{dec['panel_group']}",
                )
            )
        idx += 1

    plan = PanelPlan(
        project_slug=manifest.project_slug,
        chapter_number=manifest.chapter_number,
        items=plan_items,
    )
    atomic_write_json(chap_dir / "panel_plan.json", plan)
    apply_panel_plan(chap_dir, plan)

    # Pass 2: Re-classify ONLY newly split or merged panels (0 duplicate calls on keep/drop)
    updated_manifest = load_manifest(chap_dir)
    assert updated_manifest is not None
    to_reclassify = [
        p for p in updated_manifest.panels
        if reclassify_all or p.evidence_metadata.get("repack_op") in ("split_at", "merge")
    ]
    pass2: Dict[str, Dict[str, Any]] = {}
    if to_reclassify:
        logger.info(f"DecisionEngine Pass 2: Classifying {len(to_reclassify)} newly split/merged panels...")
        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_reclassify))) as pool:
            futs = {
                pool.submit(decide_single_panel, chap_dir / p.relative_path, p.width, p.height, key): p
                for p in to_reclassify
            }
            for fut in as_completed(futs):
                p = futs[fut]
                dec = fut.result()
                pass2[p.panel_id] = dec
                total_input_tokens += int(dec.get("usage", {}).get("total_input_tokens", 0))

    # Merge Pass 1 (for unchanged panels) + Pass 2 (for split/merged children) into final manifest
    for p in updated_manifest.panels:
        if p.panel_id in pass2:
            dec = pass2[p.panel_id]
        else:
            src_raw = (p.evidence_metadata.get("source_raw_panels") or [p.panel_id])[0]
            dec = pass1[src_raw]

        p.status = PanelStatus(dec["status"])
        p.evidence_metadata["text_detection_uncertain"] = False
        p.evidence_metadata["decision_model"] = DECISIONS_MODEL
        p.evidence_metadata["panel_group"] = dec["panel_group"]
        if p.status == PanelStatus.ACTIVE:
            p.evidence_metadata["center_crop_preset"] = dec["center_crop_2d"]
            p.evidence_metadata["display_crop"] = dec["display_crop"]
            p.evidence_metadata["tall_motion_hint"] = dec["motion_mode"]
        else:
            p.evidence_metadata.pop("display_crop", None)
            p.evidence_metadata.pop("center_crop_preset", None)
            p.evidence_metadata.pop("tall_motion_hint", None)
            if p.status == PanelStatus.CONTEXT_ONLY:
                p.notes = f"text_only | decision:{dec['panel_group']}"
            else:
                p.notes = f"skip | decision:{dec['panel_group']}"

    # Regenerate clean Contact Sheets containing ACTIVE + CONTEXT_ONLY (excluding final SKIP)
    panels_dir = chap_dir / "panels"
    sheets_dir = chap_dir / "contact_sheets"
    for old_s in sheets_dir.glob("*"):
        old_s.unlink(missing_ok=True)
    reviewable = [p for p in updated_manifest.panels if p.status != PanelStatus.SKIP]
    sheet_gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
    updated_manifest.contact_sheets = sheet_gen.generate_sheets(reviewable, panels_dir, sheets_dir)
    updated_manifest.created_at = utc_iso_now()
    atomic_write_json(chap_dir / "manifest.json", updated_manifest)

    counts = {
        "active": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.ACTIVE),
        "context_only": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.CONTEXT_ONLY),
        "skip": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.SKIP),
    }
    return {
        "project_slug": updated_manifest.project_slug,
        "chapter_number": updated_manifest.chapter_number,
        "model": DECISIONS_MODEL,
        "pass1_panels": len(panels),
        "split_resolved": len(split_cuts),
        "pass2_reclassified": len(to_reclassify),
        "final_total_panels": len(updated_manifest.panels),
        "counts_by_group": counts,
        "total_contact_sheets": len(updated_manifest.contact_sheets),
        "total_input_tokens": total_input_tokens,
    }
