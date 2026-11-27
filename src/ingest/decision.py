"""Domain panel classification, split/merge routing, 4-sided display crop, and cache pipeline."""

from __future__ import annotations

import dataclasses
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
import statistics
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from src.core.contact_sheet import ContactSheetGenerator
from src.core.errors import DomainValidationError
from src.core.logger import logger
from src.core.panel_repack import (
    PanelPlan,
    PanelPlanItem,
    PanelPlanOp,
    apply_panel_plan,
    inspect_panels_with_candidates,
)
from src.core.schemas import PanelItem, PanelManifest, PanelStatus, utc_iso_now
from src.core.settings import resolve_gemini_api_key
from src.core.storage import atomic_write_json, calculate_sha256, load_manifest
from src.providers.decision import (
    DECISION_USD_PER_1M_INPUT_TOKENS,
    DECISIONS_MODEL,
    DecisionClient,
    ProviderContentBlockedError,
    call_gemini_decision,
    encode_image_jpeg,
    extract_usage_metrics,
)
from src.render.visual_policy import SCROLL_VIEWPORT_H_OVER_W

DECISION_QUESTION_VERSION = "2026-v6.2"
CROP_POLICY_VERSION = "2026-v6.2-4sided"
CROP_PCT_OPTIONS: Tuple[int, ...] = (0, 10, 20, 30, 40)
CROP_PCT_KEYS: Tuple[str, ...] = ("0", "10", "20", "30", "40")
CROP_EDGE_NAMES: Tuple[str, ...] = ("left", "top", "right", "bottom")
REQUIRED_RAW_QUESTION_KEYS: Tuple[str, ...] = (
    "panel_group",
    "structure_action",
    "crop_left",
    "crop_top",
    "crop_right",
    "crop_bottom",
)
FULL_DISPLAY_REGION: Dict[str, float] = {"x_min": 0.0, "y_min": 0.0, "x_max": 1.0, "y_max": 1.0}


@dataclass(frozen=True)
class DecisionPolicyConfig:
    """Fixed benchmarked 4-sided adaptive configuration for Decision 1 routing and crop framing."""

    policy: str = "4sided"
    image_resolution: Optional[str] = "medium"
    adaptive_tall_resolution: Optional[str] = "high"
    max_height: int = 1024
    max_width: Optional[int] = 1280
    jpeg_quality: int = 82
    edge_conf_threshold: float = 0.60
    edge_margin_threshold: float = 0.12
    group_conf_threshold: float = 0.55
    min_crop_width: float = 0.60
    min_crop_height: float = 0.60
    min_crop_area: float = 0.50
    split_min_hw: float = 2.2
    split_min_child_ratio: float = 0.10
    split_min_child_px: int = 120
    max_merge_vcs_gap_px: int = 1400
    cost_usd_per_1m_input_tokens: float = DECISION_USD_PER_1M_INPUT_TOKENS

    def __post_init__(self) -> None:
        if self.policy != "4sided":
            raise ValueError(f"Unsupported decision policy '{self.policy}'. Only '4sided' is supported.")
        if self.image_resolution != "medium" or self.adaptive_tall_resolution != "high":
            raise ValueError("Decision input resolution must stay fixed at medium / high adaptive.")


_EDGE_CRITERIA: Dict[str, str] = {
    "0": "Keep edge: face, action, or vital text touches edge.",
    "10": "Trim 10%: outer margin, backdrop, watermark, or filler.",
    "20": "Trim 20%: outer margin, backdrop, watermark, or filler.",
    "30": "Trim 30%: wide empty margin, backdrop, or filler.",
    "40": "Trim 40%: huge blank margin or non-story edge.",
}


def build_panel_questions(
    width: int,
    height: int,
) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Builds standardized closed-set questions for single-panel routing and 4-sided edge framing."""
    hw = round(height / float(max(1, width)), 2)
    ctx = (
        f"Manga panel {width}x{height}px (H/W={hw:.2f}) for 16:9 recap display. "
        "Preserve character faces, eyes, bodies, action details, and essential story dialogue. "
        "Trim only safely dispensable outer blank margin, flat backdrop, site watermark, or outer filler. "
        "Trim percentages apply independently to original panel edges."
    )
    questions: Dict[str, Dict[str, Any]] = {
        "panel_group": {
            "type": "choice",
            "instructions": "Classify this panel into one production group.",
            "criteria": {
                "group_1_story_art": "Contains manga characters, creatures, action, effects, or scene art (even if overlaid with a site watermark).",
                "group_2_text_only": "Contains ONLY speech bubbles, narration, SFX, or title card on plain background with NO character or scene drawing.",
                "group_3_skip": "Standalone site ad banner, standalone translator watermark/logo, dotted separator, or blank transition strip with NO story artwork or dialogue.",
            },
        },
        "structure_action": {
            "type": "choice",
            "instructions": "Does this panel have a vertical segmentation defect requiring split or merge?",
            "criteria": {
                "ok_single": "Single frame, continuous tall illustration without stacked separate borders, or non-story card.",
                "needs_split": "Tall strip (typically H/W >= 2.2) with 2+ distinct stacked scenes/frames to separate.",
                "merge_with_prev": "Short cut-off sliver or split fragment belonging together with the immediately preceding panel.",
            },
        },
    }
    for side in CROP_EDGE_NAMES:
        questions[f"crop_{side}"] = {
            "type": "choice",
            "instructions": (
                f"How much of the {side.upper()} edge of this original image can be safely trimmed "
                "(0-40%) without clipping faces, bodies, action, or essential dialogue?"
            ),
            "criteria": dict(_EDGE_CRITERIA),
        }
    return ctx, questions


def compute_decision_cache_version(
    model: str = DECISIONS_MODEL,
    *,
    config: Optional[DecisionPolicyConfig] = None,
) -> str:
    """Computes a stable version stamp over model + 4-sided question contract + preprocess config."""
    cfg = config or DecisionPolicyConfig()
    _, sample_questions = build_panel_questions(1000, 1000)
    canonical_spec: Dict[str, Any] = {
        "model": model,
        "question_version": DECISION_QUESTION_VERSION,
        "policy": "4sided",
        "crop_policy_version": CROP_POLICY_VERSION,
        "crop_pct_options": list(CROP_PCT_OPTIONS),
        "questions": sample_questions,
        "preprocess": {
            "max_height": cfg.max_height,
            "max_width": cfg.max_width,
            "jpeg_quality": cfg.jpeg_quality,
            "image_resolution": cfg.image_resolution,
            "adaptive_tall_resolution": cfg.adaptive_tall_resolution,
        },
    }
    digest = hashlib.sha256(
        json.dumps(canonical_spec, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"{model}:{DECISION_QUESTION_VERSION}:4sided:{digest}"


def crop_edges_to_region(left: int, top: int, right: int, bottom: int) -> Dict[str, float]:
    """Maps integer percentage edge trims `(L, T, R, B)` in `{0,10,20,30,40}` to canonical Region."""
    for name, val in zip(CROP_EDGE_NAMES, (left, top, right, bottom)):
        if val not in CROP_PCT_OPTIONS:
            raise ValueError(f"Unsupported crop percentage {val} for edge '{name}'; expected one of {CROP_PCT_OPTIONS}.")
    x_min = round(left / 100.0, 4)
    y_min = round(top / 100.0, 4)
    x_max = round(1.0 - (right / 100.0), 4)
    y_max = round(1.0 - (bottom / 100.0), 4)
    if x_min >= x_max or y_min >= y_max:
        raise ValueError(f"Invalid collapsed crop region from edges L={left}, T={top}, R={right}, B={bottom}.")
    return {"x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max}


def _select_conservative_edge_trim(
    ans_obj: Any,
    *,
    edge_conf_threshold: float,
    edge_margin_threshold: float,
    apply_guardrails: bool,
) -> Tuple[Optional[int], int, Optional[float], Optional[float], Dict[int, float]]:
    """
    Parses and backs off a single edge trim option conservatively.
    Returns `(raw_pct_or_None, accepted_pct, confidence_or_None, support_score_or_None, cumulative_support)`.
    """
    if not isinstance(ans_obj, dict):
        return None, 0, None, None, {}
    choice = ans_obj.get("choice")
    if not isinstance(choice, str) or choice not in CROP_PCT_KEYS:
        return None, 0, None, None, {}
    raw_pct = int(choice)
    conf_raw = ans_obj.get("confidence")
    conf = float(conf_raw) if isinstance(conf_raw, (int, float)) and not isinstance(conf_raw, bool) else None
    probs = ans_obj.get("probabilities") if isinstance(ans_obj.get("probabilities"), dict) else {}

    cum_support: Dict[int, float] = {}
    if probs:
        for k in CROP_PCT_OPTIONS:
            cum_support[k] = round(
                sum(float(probs.get(str(j), 0.0) or 0.0) for j in CROP_PCT_OPTIONS if j >= k),
                6,
            )

    if not apply_guardrails:
        return raw_pct, raw_pct, conf, cum_support.get(raw_pct, conf), cum_support

    if raw_pct == 0:
        return 0, 0, conf, cum_support.get(0, conf), cum_support

    if probs:
        p_zero = float(probs.get("0", 0.0) or 0.0)
        p_chosen = float(probs.get(choice, 0.0) or 0.0)
        sorted_probs = sorted((float(v or 0.0) for v in probs.values()), reverse=True)
        margin = (sorted_probs[0] - sorted_probs[1]) if len(sorted_probs) >= 2 else sorted_probs[0]
        accepted = 0
        for candidate in (40, 30, 20, 10, 0):
            if candidate > raw_pct:
                continue
            if candidate == 0:
                accepted = 0
                break
            sup = cum_support.get(candidate, 0.0)
            # Compare cumulative safe-trim support P(trim >= candidate) against 0% keep mass + margin
            if candidate == raw_pct and (sup - p_zero) < edge_margin_threshold:
                continue
            if sup >= edge_conf_threshold and (candidate < raw_pct or margin >= edge_margin_threshold or (sup - p_zero) >= edge_margin_threshold):
                accepted = candidate
                break
        support_val = cum_support.get(accepted, conf)
        return raw_pct, accepted, conf, support_val, cum_support

    if conf is None or conf < edge_conf_threshold:
        return raw_pct, 0, conf, conf, cum_support

    return raw_pct, raw_pct, conf, conf, cum_support


def evaluate_4sided_crop(
    answers: Dict[str, Any],
    width: int,
    height: int,
    *,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    """
    Combines 4 independent edge trim choices into a safe normalized `display_crop` box.
    Applies confidence backoff plus `min_width` / `min_height` / `min_area` guardrails.
    """
    cfg = config or DecisionPolicyConfig(policy="4sided")
    raw_edges: Dict[str, int] = {}
    edges: Dict[str, int] = {}
    edge_confs: Dict[str, Optional[float]] = {}
    edge_supports: Dict[str, Optional[float]] = {}
    valid = True

    for side in CROP_EDGE_NAMES:
        raw_pct, acc_pct, conf, sup, _ = _select_conservative_edge_trim(
            answers.get(f"crop_{side}"),
            edge_conf_threshold=cfg.edge_conf_threshold,
            edge_margin_threshold=cfg.edge_margin_threshold,
            apply_guardrails=True,
        )
        if raw_pct is None:
            valid = False
            raw_edges[side] = 0
            edges[side] = 0
            edge_confs[side] = None
            edge_supports[side] = None
        else:
            raw_edges[side] = raw_pct
            edges[side] = acc_pct
            edge_confs[side] = round(conf, 4) if conf is not None else None
            edge_supports[side] = round(sup, 4) if sup is not None else None

    if not valid:
        box = crop_edges_to_region(0, 0, 0, 0)
        return {
            "decision_valid": False,
            "crop_safe": False,
            "raw_crop_edges_pct": raw_edges,
            "crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "display_crop": box,
            "retained_width": 1.0,
            "retained_height": 1.0,
            "retained_area": 1.0,
            "guardrail_adjusted": raw_edges != {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "crop_confidence": {"edges": edge_confs, "min_edge": None},
            "motion_mode": compute_deterministic_motion(width, height, box),
        }

    # Apply retention guardrails: iteratively shrink weakest non-zero active trim by 10%
    guardrail_adjusted = edges != raw_edges
    for _ in range(20):
        w_frac = 1.0 - (edges["left"] + edges["right"]) / 100.0
        h_frac = 1.0 - (edges["top"] + edges["bottom"]) / 100.0
        a_frac = w_frac * h_frac
        need_w = w_frac < cfg.min_crop_width - 1e-9
        need_h = h_frac < cfg.min_crop_height - 1e-9
        need_a = a_frac < cfg.min_crop_area - 1e-9
        if not (need_w or need_h or need_a):
            break
        candidates: List[str] = []
        if need_w:
            candidates.extend([s for s in ("left", "right") if edges[s] > 0])
        if need_h and not candidates:
            candidates.extend([s for s in ("top", "bottom") if edges[s] > 0])
        if not candidates:
            candidates = [s for s in CROP_EDGE_NAMES if edges[s] > 0]
        if not candidates:
            break
        weakest = min(
            candidates,
            key=lambda s: (
                edge_supports[s] if edge_supports[s] is not None else 0.0,
                -edges[s],
                s,
            ),
        )
        edges[weakest] = max(0, edges[weakest] - 10)
        guardrail_adjusted = True

    w_frac = round(1.0 - (edges["left"] + edges["right"]) / 100.0, 4)
    h_frac = round(1.0 - (edges["top"] + edges["bottom"]) / 100.0, 4)
    a_frac = round(w_frac * h_frac, 4)
    if w_frac < cfg.min_crop_width - 1e-9 or h_frac < cfg.min_crop_height - 1e-9 or a_frac < cfg.min_crop_area - 1e-9:
        edges = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        w_frac = h_frac = a_frac = 1.0
        guardrail_adjusted = True

    box = crop_edges_to_region(edges["left"], edges["top"], edges["right"], edges["bottom"])
    active_confs = [c for s, c in edge_confs.items() if c is not None and (edges[s] > 0 or raw_edges[s] > 0)]
    all_confs = [c for c in edge_confs.values() if c is not None]
    min_edge_conf = min(active_confs) if active_confs else (min(all_confs) if all_confs else None)

    return {
        "decision_valid": True,
        "crop_safe": True,
        "raw_crop_edges_pct": raw_edges,
        "crop_edges_pct": edges,
        "display_crop": box,
        "retained_width": w_frac,
        "retained_height": h_frac,
        "retained_area": a_frac,
        "guardrail_adjusted": guardrail_adjusted,
        "crop_confidence": {
            "edges": edge_confs,
            "supports": edge_supports,
            "min_edge": round(min_edge_conf, 4) if min_edge_conf is not None else None,
        },
        "motion_mode": compute_deterministic_motion(width, height, box),
    }


def compute_deterministic_motion(width: int, height: int, display_crop: Dict[str, float]) -> str:
    """Computes static vs downward scroll deterministically from accepted display crop dimensions."""
    crop_w = max(1.0, float(width) * float(display_crop["x_max"] - display_crop["x_min"]))
    crop_h = max(1.0, float(height) * float(display_crop["y_max"] - display_crop["y_min"]))
    return "scroll" if (crop_h / crop_w) > SCROLL_VIEWPORT_H_OVER_W else "static"


def _blocked_raw_record(reason: str) -> Dict[str, Any]:
    return {
        "blocked": True,
        "review_reason": reason,
        "answers": {},
        "usage": {},
        "_meta": {"usage_known": False, "retries": 0, "latency_ms": 0.0},
    }


def interpret_panel_answers(
    raw_payload: Dict[str, Any],
    width: int,
    height: int,
    *,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    """
    Pure, deterministic 4-sided interpretation over raw API answers so thresholds can be
    re-evaluated offline via `--reapply-policy` without re-calling Gemini Decisions API.
    """
    cfg = config or DecisionPolicyConfig()
    if raw_payload.get("blocked"):
        full_box = dict(FULL_DISPLAY_REGION)
        return {
            "panel_group": "needs_review",
            "status": PanelStatus.NEEDS_REVIEW.value,
            "structure_action": "ok_single",
            "crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "raw_crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "display_crop": full_box,
            "motion_mode": "static",
            "decision_valid": False,
            "crop_safe": False,
            "text_detection_uncertain": True,
            "group_confidence": None,
            "structure_confidence": None,
            "crop_confidence": None,
            "crop_method": "4sided_pct",
            "review_reason": raw_payload.get("review_reason") or "content_blocked",
            "usage": raw_payload.get("usage", {}),
            "_meta": raw_payload.get("_meta", {}),
        }

    answers = raw_payload.get("answers") if isinstance(raw_payload.get("answers"), dict) else {}
    group_obj = answers.get("panel_group") if isinstance(answers.get("panel_group"), dict) else {}
    struct_obj = answers.get("structure_action") if isinstance(answers.get("structure_action"), dict) else {}

    group_key = group_obj.get("choice")
    struct_key = struct_obj.get("choice")
    group_conf_raw = group_obj.get("confidence")
    group_conf = (
        float(group_conf_raw)
        if isinstance(group_conf_raw, (int, float)) and not isinstance(group_conf_raw, bool)
        else None
    )
    if group_conf is None and isinstance(group_obj.get("probabilities"), dict) and isinstance(group_key, str):
        prob_raw = group_obj["probabilities"].get(group_key)
        if isinstance(prob_raw, (int, float)) and not isinstance(prob_raw, bool):
            group_conf = float(prob_raw)
    struct_conf_raw = struct_obj.get("confidence")
    struct_conf = (
        float(struct_conf_raw)
        if isinstance(struct_conf_raw, (int, float)) and not isinstance(struct_conf_raw, bool)
        else None
    )

    group_map = {
        "group_1_story_art": PanelStatus.ACTIVE,
        "group_2_text_only": PanelStatus.CONTEXT_ONLY,
        "group_3_skip": PanelStatus.SKIP,
    }
    valid_group = group_key in group_map
    valid_struct = struct_key in ("ok_single", "needs_split", "merge_with_prev")

    missing_group_conf = group_conf is None
    low_group_conf = group_conf is not None and group_conf < cfg.group_conf_threshold
    if not valid_group or not valid_struct or missing_group_conf or low_group_conf:
        reason = (
            "invalid_group_or_structure"
            if not (valid_group and valid_struct)
            else ("missing_group_confidence" if missing_group_conf else f"low_group_confidence:{group_conf:.3f}")
        )
        full_box = dict(FULL_DISPLAY_REGION)
        return {
            "panel_group": group_key or "uncertain",
            "status": PanelStatus.NEEDS_REVIEW.value,
            "structure_action": "ok_single",
            "crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "raw_crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "display_crop": full_box,
            "motion_mode": "static",
            "decision_valid": False,
            "crop_safe": False,
            "text_detection_uncertain": True,
            "group_confidence": round(group_conf, 4) if group_conf is not None else None,
            "structure_confidence": round(struct_conf, 4) if struct_conf is not None else None,
            "crop_confidence": None,
            "crop_method": "4sided_pct",
            "review_reason": reason,
            "usage": raw_payload.get("usage", {}),
            "_meta": raw_payload.get("_meta", {}),
        }

    status = group_map[str(group_key)]
    eff_struct = str(struct_key)
    hw = height / float(max(1, width))
    if eff_struct == "needs_split" and (status != PanelStatus.ACTIVE or hw < cfg.split_min_hw):
        eff_struct = "ok_single"

    if status != PanelStatus.ACTIVE:
        full_box = dict(FULL_DISPLAY_REGION)
        return {
            "panel_group": str(group_key),
            "status": status.value,
            "structure_action": eff_struct if eff_struct == "merge_with_prev" else "ok_single",
            "crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "raw_crop_edges_pct": {"left": 0, "top": 0, "right": 0, "bottom": 0},
            "display_crop": full_box,
            "motion_mode": "static",
            "decision_valid": True,
            "crop_safe": True,
            "text_detection_uncertain": False,
            "group_confidence": round(group_conf, 4) if group_conf is not None else None,
            "structure_confidence": round(struct_conf, 4) if struct_conf is not None else None,
            "crop_confidence": None,
            "crop_method": "4sided_pct",
            "review_reason": None,
            "usage": raw_payload.get("usage", {}),
            "_meta": raw_payload.get("_meta", {}),
        }

    crop_res = evaluate_4sided_crop(answers, width, height, config=cfg)
    return {
        "panel_group": str(group_key),
        "status": PanelStatus.ACTIVE.value,
        "structure_action": eff_struct,
        "crop_edges_pct": crop_res["crop_edges_pct"],
        "raw_crop_edges_pct": crop_res["raw_crop_edges_pct"],
        "display_crop": crop_res["display_crop"],
        "retained_width": crop_res["retained_width"],
        "retained_height": crop_res["retained_height"],
        "retained_area": crop_res["retained_area"],
        "guardrail_adjusted": crop_res["guardrail_adjusted"],
        "motion_mode": crop_res["motion_mode"] if crop_res["decision_valid"] else "static",
        "decision_valid": crop_res["decision_valid"],
        "crop_safe": crop_res["crop_safe"],
        "text_detection_uncertain": not crop_res["decision_valid"],
        "group_confidence": round(group_conf, 4) if group_conf is not None else None,
        "structure_confidence": round(struct_conf, 4) if struct_conf is not None else None,
        "crop_confidence": crop_res["crop_confidence"],
        "crop_method": "4sided_pct",
        "review_reason": None if crop_res["decision_valid"] else "invalid_crop_answer",
        "usage": raw_payload.get("usage", {}),
        "_meta": raw_payload.get("_meta", {}),
    }


def _resolve_panel_resolution(width: int, height: int, cfg: DecisionPolicyConfig) -> Optional[str]:
    hw = height / float(max(1, width))
    if cfg.adaptive_tall_resolution and hw >= cfg.split_min_hw:
        return cfg.adaptive_tall_resolution
    return cfg.image_resolution


def _evaluate_panel_raw(
    image_path: Path,
    width: int,
    height: int,
    api_key: str,
    *,
    client: Optional[DecisionClient] = None,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    cfg = config or DecisionPolicyConfig()
    ctx, questions = build_panel_questions(width, height)
    eff_resolution = _resolve_panel_resolution(width, height, cfg)
    try:
        res = call_gemini_decision(
            image_path=image_path,
            text_context=ctx,
            questions=questions,
            api_key=api_key,
            max_height=cfg.max_height,
            max_width=cfg.max_width,
            quality=cfg.jpeg_quality,
            image_resolution=eff_resolution,
            client=client,
        )
        return dict(res)
    except ProviderContentBlockedError as exc:
        logger.warning(f"Decision API safety block on {image_path.name}: {exc}")
        return _blocked_raw_record(f"content_blocked:{exc.error_code}")


def decide_single_panel(
    image_path: Path,
    width: int,
    height: int,
    api_key: str,
    *,
    client: Optional[DecisionClient] = None,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    """Runs Decision 1 evaluation for a single panel image using canonical 4-sided framing."""
    cfg = config or DecisionPolicyConfig()
    raw_res = _evaluate_panel_raw(
        image_path,
        width,
        height,
        api_key,
        client=client,
        config=cfg,
    )
    dec = interpret_panel_answers(raw_res, width, height, config=cfg)
    dec["raw_answers"] = raw_res.get("answers", {})
    return dec


def _choose_cut_ratios_detailed(
    chap_dir: Path,
    panel_id: str,
    hw_ratio: float,
    api_key: str,
    *,
    client: Optional[DecisionClient] = None,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    """Draws numbered cut lines on a tall panel and asks Gemini Decision 1 to pick clean cut(s)."""
    cfg = config or DecisionPolicyConfig()
    insp = inspect_panels_with_candidates(chap_dir, [panel_id], max_candidates=8)["inspected"][0]
    cands = insp["candidates"]
    if not cands:
        return {"cuts": None, "candidates": 0, "reason": "no_candidates", "usage": {}, "_meta": {}}
    inspect_path = chap_dir / insp["inspect_image"]
    panel_h = max(1, int(insp["height"]))

    criteria: Dict[str, str] = {}
    for c in cands:
        criteria[f"cut_{c['index']}"] = (
            f"Split cleanly at horizontal red line CUT #{c['index']} (y_ratio={c['ratio']:.3f}) "
            "inside a white/empty transition gap between two stacked comic frames without cutting through faces."
        )
    # Always offer no_split even if H/W >= 4.0 so continuous art is never forced-cut.
    criteria["no_split"] = (
        "Keep panel intact without splitting if it is one continuous illustration or every red cut line cuts faces, bodies, or dialogue."
    )

    questions: Dict[str, Dict[str, Any]] = {
        "primary_cut": {
            "type": "choice",
            "instructions": (
                "Inspect the horizontal red lines labeled CUT #1..#N. Pick the ONE cut line that lies "
                "in a blank/transition gap between two distinct frames, or choose no_split if continuous."
            ),
            "criteria": criteria,
        }
    }
    if hw_ratio >= 5.2 and len(cands) >= 3:
        sec_criteria = {"none": "No second split needed; one split is sufficient."}
        for c in cands:
            sec_criteria[f"cut_{c['index']}"] = (
                f"Also split at CUT #{c['index']} (y_ratio={c['ratio']:.3f}) to separate a 3rd distinct frame."
            )
        questions["secondary_cut"] = {
            "type": "choice",
            "instructions": (
                "Because this strip is very tall, pick a second cut line in another gap if there are "
                "3 distinct frames, else choose none."
            ),
            "criteria": sec_criteria,
        }

    try:
        res = call_gemini_decision(
            image_path=inspect_path,
            text_context=f"Annotated tall panel {panel_id} (H/W={hw_ratio:.2f}) with candidate cut lines.",
            questions=questions,
            api_key=api_key,
            max_height=2048,
            quality=cfg.jpeg_quality,
            image_resolution=cfg.image_resolution,
            client=client,
        )
    except ProviderContentBlockedError as exc:
        logger.warning(f"Decision split safety block on {panel_id}: {exc}")
        return {"cuts": None, "candidates": len(cands), "reason": f"blocked:{exc.error_code}", "usage": {}, "_meta": {}}

    answers = res.get("answers", {})
    p_choice = answers.get("primary_cut", {}).get("choice", "no_split")
    if p_choice == "no_split":
        return {
            "cuts": None,
            "candidates": len(cands),
            "reason": "model_no_split",
            "usage": res.get("usage", {}),
            "_meta": res.get("_meta", {}),
        }

    cand_map = {f"cut_{c['index']}": float(c["ratio"]) for c in cands}
    chosen: List[float] = []
    if p_choice in cand_map:
        chosen.append(cand_map[p_choice])
    s_choice = answers.get("secondary_cut", {}).get("choice", "none")
    if s_choice in cand_map and cand_map[s_choice] not in chosen:
        if all(abs(cand_map[s_choice] - r) >= 0.12 for r in chosen):
            chosen.append(cand_map[s_choice])
    chosen.sort()

    # Validate minimum child height constraints
    if chosen:
        min_frac = max(cfg.split_min_child_ratio, cfg.split_min_child_px / float(panel_h))
        bounds = [0.0] + chosen + [1.0]
        valid_cuts = all((b - a) >= min_frac for a, b in zip(bounds, bounds[1:]))
        if not valid_cuts:
            filtered = [r for r in chosen if min_frac <= r <= (1.0 - min_frac)]
            chosen = filtered

    return {
        "cuts": chosen or None,
        "candidates": len(cands),
        "reason": "split_selected" if chosen else "guardrail_no_valid_cut",
        "usage": res.get("usage", {}),
        "_meta": res.get("_meta", {}),
    }


def choose_cut_ratios_via_decision(
    chap_dir: Path,
    panel_id: str,
    hw_ratio: float,
    api_key: str,
    *,
    client: Optional[DecisionClient] = None,
    config: Optional[DecisionPolicyConfig] = None,
) -> Optional[List[float]]:
    """Draws numbered cut lines on a tall multi-frame panel and asks Gemini Decision 1 to pick cut(s)."""
    return _choose_cut_ratios_detailed(
        chap_dir,
        panel_id,
        hw_ratio,
        api_key,
        client=client,
        config=config,
    )["cuts"]


def _is_vcs_merge_contiguous(prev_panel: PanelItem, curr_panel: PanelItem, *, max_gap_px: int) -> Tuple[bool, Optional[int], str]:
    if prev_panel.vcs_end is None or curr_panel.vcs_start is None:
        return False, None, "missing_vcs_coords"
    gap = int(curr_panel.vcs_start) - int(prev_panel.vcs_end)
    if gap < -8:
        return False, gap, "non_monotonic_vcs"
    if gap > max_gap_px:
        return False, gap, f"vcs_gap_too_large:{gap}px"
    return True, gap, "vcs_contiguous"


def verify_merge_pair_via_decision(
    chap_dir: Path,
    prev_panel: PanelItem,
    curr_panel: PanelItem,
    vcs_gap_px: int,
    api_key: str,
    *,
    client: DecisionClient,
    config: DecisionPolicyConfig,
) -> Dict[str, Any]:
    """Verifies whether `curr_panel` truly forms one broken frame with `prev_panel` using composite context."""
    prev_path = chap_dir / prev_panel.relative_path
    curr_path = chap_dir / curr_panel.relative_path
    with Image.open(prev_path) as im_p, Image.open(curr_path) as im_c:
        p_rgb = im_p.convert("RGB")
        c_rgb = im_c.convert("RGB")
        # Crop bottom tail of prev (up to 900px) + top/full curr to focus cleanly around the boundary
        if p_rgb.height > 900:
            p_rgb = p_rgb.crop((0, p_rgb.height - 900, p_rgb.width, p_rgb.height))
        if c_rgb.height > 900:
            c_rgb = c_rgb.crop((0, 0, c_rgb.width, 900))
        w = max(p_rgb.width, c_rgb.width)
        bar_h = 28
        comp = Image.new("RGB", (w, p_rgb.height + bar_h + c_rgb.height), (255, 255, 255))
        comp.paste(p_rgb, (0, 0))
        draw = ImageDraw.Draw(comp)
        draw.rectangle([0, p_rgb.height, w, p_rgb.height + bar_h], fill=(220, 50, 30))
        draw.text((12, p_rgb.height + 6), f"BOUNDARY (VCS gap={vcs_gap_px}px): TOP=prev | BOTTOM=curr", fill=(255, 255, 255))
        comp.paste(c_rgb, (0, p_rgb.height + bar_h))
        if comp.height > config.max_height:
            sc = config.max_height / float(comp.height)
            comp = comp.resize((max(1, int(round(comp.width * sc))), config.max_height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        comp.save(buf, format="JPEG", quality=config.jpeg_quality)
    import base64

    raw_bytes = buf.getvalue()
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    img_block: Dict[str, Any] = {"type": "image", "data": b64, "mime_type": "image/jpeg"}
    if config.image_resolution:
        img_block["resolution"] = config.image_resolution
    questions = {
        "merge_decision": {
            "type": "choice",
            "instructions": (
                "TOP is the end of the previous panel and BOTTOM is the next segment separated by the red boundary bar. "
                "Should BOTTOM be merged back with TOP into one single scene/speech panel?"
            ),
            "criteria": {
                "merge": "BOTTOM is clearly a sliced continuation, split speech bubble, or torn fragment of TOP.",
                "keep_separate": "TOP and BOTTOM are distinct complete panels, scenes, or separate speech turns.",
                "uncertain": "Boundary relation is ambiguous; keep separate and flag for review.",
            },
        }
    }
    try:
        res = client.call_input(
            input_blocks=[img_block, {"type": "text", "text": f"Merge check {prev_panel.panel_id} + {curr_panel.panel_id}, vcs_gap_px={vcs_gap_px}."}],
            questions=questions,
            api_key=api_key,
            payload_bytes=len(raw_bytes),
        )
        ans = res["answers"]["merge_decision"]
        return {
            "action": str(ans["choice"]),
            "confidence": ans.get("confidence"),
            "usage": res.get("usage", {}),
            "_meta": res.get("_meta", {}),
        }
    except ProviderContentBlockedError as exc:
        return {"action": "uncertain", "confidence": None, "reason": f"blocked:{exc.error_code}", "usage": {}, "_meta": {}}


def _record_usage_and_latency(telemetry: Dict[str, Any], phase: str, usage: Any, meta: Any) -> None:
    telemetry["requests"][phase] = telemetry["requests"].get(phase, 0) + 1
    u = usage if isinstance(usage, dict) else {}
    m = meta if isinstance(meta, dict) else {}
    telemetry["retries"] += int(m.get("retries") or 0)
    if "latency_ms" in m and isinstance(m["latency_ms"], (int, float)):
        telemetry["latencies_ms"].append(float(m["latency_ms"]))
    parsed = extract_usage_metrics({"usage": u}) if u else {"usage_known": False}
    if not parsed.get("usage_known"):
        telemetry["unknown_usage_requests"] += 1
        return
    tot = int(parsed["total_input_tokens"] or 0)
    telemetry["total_input_tokens"] += tot
    telemetry["tokens_by_phase"][phase] = telemetry["tokens_by_phase"].get(phase, 0) + tot
    if parsed.get("text_tokens") is not None:
        telemetry["text_tokens"] += int(parsed["text_tokens"])
    if parsed.get("image_tokens") is not None:
        telemetry["image_tokens"] += int(parsed["image_tokens"])


def _verify_panel_disk_image(chap_dir: Path, p: PanelItem) -> Tuple[Path, str, int, int]:
    """Verifies on-disk panel existence, SHA-256 bytes, and image dimensions before cache/API use."""
    root = chap_dir.resolve()
    panels_root = root / "panels"
    img_path = (root / p.relative_path).resolve()
    if not img_path.is_relative_to(panels_root) or not img_path.is_file() or img_path.stat().st_size <= 0:
        raise DomainValidationError(
            f"Missing or unreadable panel file '{p.relative_path}' for '{p.panel_id}'; re-run 'segment --force'."
        )
    actual_sha = calculate_sha256(img_path)
    if actual_sha != p.sha256:
        raise DomainValidationError(
            f"Panel '{p.panel_id}' byte checksum mismatch on disk; re-run 'segment --force'."
        )
    try:
        with Image.open(img_path) as im:
            im.load()
            actual_w, actual_h = im.size
    except Exception as exc:
        raise DomainValidationError(
            f"Corrupt panel image '{p.panel_id}' on disk ({exc}); re-run 'segment --force'."
        ) from exc
    if (actual_w, actual_h) != (p.width, p.height):
        raise DomainValidationError(
            f"Panel '{p.panel_id}' dimensions mismatch (disk {actual_w}x{actual_h} != manifest {p.width}x{p.height}); re-run 'segment --force'."
        )
    return img_path, actual_sha, actual_w, actual_h


def _is_complete_raw_answers(answers: Any) -> bool:
    if not isinstance(answers, dict):
        return False
    for q_key in REQUIRED_RAW_QUESTION_KEYS:
        ans = answers.get(q_key)
        if not isinstance(ans, dict):
            return False
        choice = ans.get("choice")
        if not isinstance(choice, str) or not choice.strip():
            return False
    return True


def _is_valid_raw_cache_entry(
    p: PanelItem,
    entry: Any,
    cache_version: str,
    *,
    expect_child_pass2: bool = False,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("sha256") != p.sha256:
        return False
    if entry.get("width") != p.width or entry.get("height") != p.height:
        return False
    if entry.get("cache_version") != cache_version:
        return False
    if expect_child_pass2:
        if entry.get("panel_origin") != "child_pass2":
            return False
        meta = p.evidence_metadata or {}
        if "source_raw_panels" in entry and entry.get("source_raw_panels") != meta.get("source_raw_panels"):
            return False
        if "source_raw_sha256s" in entry and entry.get("source_raw_sha256s") != meta.get("source_raw_sha256s"):
            return False
    blocked_ok = bool(entry.get("blocked")) and isinstance(entry.get("review_reason"), str) and bool(entry.get("review_reason"))
    return blocked_ok or _is_complete_raw_answers(entry.get("raw_answers"))


def _raw_payload_from_cache_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    if entry.get("blocked"):
        return _blocked_raw_record(str(entry.get("review_reason") or "content_blocked"))
    return {"answers": entry["raw_answers"], "usage": {}, "_meta": {}}


def _is_valid_split_cache_entry(p: PanelItem, entry: Any, cache_version: str) -> bool:
    if not isinstance(entry, dict):
        return False
    if (
        entry.get("sha256") != p.sha256
        or entry.get("width") != p.width
        or entry.get("height") != p.height
        or entry.get("cache_version") != cache_version
    ):
        return False
    cuts = entry.get("cuts")
    if cuts is None:
        return bool(entry.get("reason"))
    return (
        isinstance(cuts, list)
        and len(cuts) > 0
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) and 0.0 < float(c) < 1.0 for c in cuts)
    )


def _resolve_post_repack_record(
    p: PanelItem,
    pass1_records: Dict[str, Dict[str, Any]],
    pass2_records: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    """Resolves verified post-repack decision & raw payload using explicit lineage before panel_id."""
    meta = p.evidence_metadata or {}
    repack_op = meta.get("repack_op")
    if repack_op in ("split_at", "merge"):
        rec = pass2_records.get(p.panel_id)
        if rec is not None and rec["sha256"] == p.sha256 and (rec["width"], rec["height"]) == (p.width, p.height):
            return dict(rec["dec"]), rec["raw"], "child_pass2"
        p1_rec = pass1_records.get(p.panel_id)
        if (
            p1_rec is not None
            and p1_rec.get("origin") == "child_pass2"
            and p1_rec["sha256"] == p.sha256
            and (p1_rec["width"], p1_rec["height"]) == (p.width, p.height)
            and p1_rec.get("source_raw_panels") == meta.get("source_raw_panels")
            and p1_rec.get("source_raw_sha256s") == meta.get("source_raw_sha256s")
        ):
            dec_copy = dict(p1_rec["dec"])
            dec_copy["structure_action"] = "ok_single"
            return dec_copy, p1_rec["raw"], "child_pass2"
        raise DomainValidationError(
            f"Missing child Pass 2 decision for repacked panel '{p.panel_id}'; refusing parent fallback."
        )

    if repack_op in ("keep", "drop"):
        src_ids = meta.get("source_raw_panels") or []
        src_shas = meta.get("source_raw_sha256s") or []
        if len(src_ids) != 1 or len(src_shas) != 1:
            raise DomainValidationError(
                f"Invalid source lineage metadata on repacked panel '{p.panel_id}'; run 'segment --force' or repair panel_plan."
            )
        src_id, src_sha = str(src_ids[0]), str(src_shas[0])
        self_rec = pass1_records.get(p.panel_id)
        if (
            self_rec is not None
            and self_rec["sha256"] == p.sha256 == src_sha
            and (self_rec["width"], self_rec["height"]) == (p.width, p.height)
            and self_rec.get("source_raw_panels") == src_ids
            and self_rec.get("source_raw_sha256s") == src_shas
        ):
            return dict(self_rec["dec"]), self_rec["raw"], str(self_rec.get("origin") or "raw_pass1")
        src_rec = pass1_records.get(src_id)
        if (
            src_rec is None
            or src_rec["sha256"] != src_sha
            or src_sha != p.sha256
            or (src_rec["width"], src_rec["height"]) != (p.width, p.height)
        ):
            raise DomainValidationError(
                f"Lineage mismatch for repacked panel '{p.panel_id}' (expected source '{src_id}' / {src_sha[:8]})."
            )
        return dict(src_rec["dec"]), src_rec["raw"], str(src_rec.get("origin") or "raw_pass1")

    rec = pass1_records.get(p.panel_id)
    if rec is None or rec["sha256"] != p.sha256 or (rec["width"], rec["height"]) != (p.width, p.height):
        raise DomainValidationError(
            f"Missing verified Pass 1 decision for panel '{p.panel_id}' ({p.sha256[:8]})."
        )
    return dict(rec["dec"]), rec["raw"], str(rec.get("origin") or "raw_pass1")


def _apply_decision_to_panel(
    p: PanelItem,
    dec: Dict[str, Any],
    *,
    model_name: str,
    cfg: DecisionPolicyConfig,
    needs_review_panels: List[Dict[str, Any]],
) -> None:
    """Applies interpreted decision fields cleanly onto a manifest PanelItem."""
    p.status = PanelStatus(dec["status"])
    p.evidence_metadata["text_detection_uncertain"] = bool(dec.get("text_detection_uncertain", False))
    p.evidence_metadata["crop_safe"] = bool(dec.get("crop_safe", True))
    p.evidence_metadata["decision_valid"] = bool(dec.get("decision_valid", True))
    p.evidence_metadata["decision_model"] = model_name
    p.evidence_metadata["decision_policy"] = cfg.policy
    p.evidence_metadata["panel_group"] = dec["panel_group"]
    if dec.get("group_confidence") is not None:
        p.evidence_metadata["group_confidence"] = dec["group_confidence"]
    else:
        p.evidence_metadata.pop("group_confidence", None)

    if p.status == PanelStatus.ACTIVE:
        p.evidence_metadata.pop("review_reason", None)
        p.evidence_metadata.pop("center_crop_preset", None)
        if dec.get("crop_edges_pct") is not None:
            p.evidence_metadata["crop_edges_pct"] = dec["crop_edges_pct"]
        else:
            p.evidence_metadata.pop("crop_edges_pct", None)
        if dec.get("raw_crop_edges_pct") is not None:
            p.evidence_metadata["raw_crop_edges_pct"] = dec["raw_crop_edges_pct"]
        else:
            p.evidence_metadata.pop("raw_crop_edges_pct", None)
        p.evidence_metadata["crop_method"] = dec.get("crop_method", "4sided_pct")
        if dec.get("crop_confidence") is not None:
            p.evidence_metadata["crop_confidence"] = dec["crop_confidence"]
        else:
            p.evidence_metadata.pop("crop_confidence", None)
        p.evidence_metadata["crop_policy_version"] = CROP_POLICY_VERSION
        p.evidence_metadata["display_crop"] = dec["display_crop"]
        p.evidence_metadata["tall_motion_hint"] = dec["motion_mode"]
        if isinstance(p.notes, str) and p.notes.startswith(("needs_review |", "skip |", "text_only |")):
            p.notes = f"decision_{dec['panel_group']}"
    else:
        for drop_k in (
            "display_crop",
            "center_crop_preset",
            "crop_edges_pct",
            "raw_crop_edges_pct",
            "crop_method",
            "crop_confidence",
            "crop_policy_version",
            "tall_motion_hint",
        ):
            p.evidence_metadata.pop(drop_k, None)
        if p.status == PanelStatus.CONTEXT_ONLY:
            p.evidence_metadata.pop("review_reason", None)
            p.notes = f"text_only | decision:{dec['panel_group']}"
        elif p.status == PanelStatus.SKIP:
            p.evidence_metadata.pop("review_reason", None)
            p.notes = f"skip | decision:{dec['panel_group']}"
        else:
            reason = dec.get("review_reason") or "uncertain_decision"
            p.evidence_metadata["review_reason"] = reason
            p.notes = f"needs_review | decision:{reason}"
            needs_review_panels.append({"panel_id": p.panel_id, "reason": reason})


def run_decision_pipeline(
    chapter_dir: Path,
    *,
    api_key: Optional[str] = None,
    max_workers: int = 3,
    refresh: bool = False,
    client: Optional[DecisionClient] = None,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    """
    2-Pass non-duplicative Decision API pipeline:
      Pass 1: Evaluate raw panels (group 1/2/3, split/merge candidate, 4-sided crop).
      Split/Merge: Guarded resolution via VCS continuity + semantic cut/merge checks.
      Pass 2: Re-classify ONLY newly created split/merged child panels.
    """
    cfg = config or DecisionPolicyConfig()
    chap_dir = Path(chapter_dir)
    manifest = load_manifest(chap_dir)
    if manifest is None or not manifest.panels:
        raise ValueError(f"Missing or empty manifest.json in {chap_dir}; run 'segment' first.")

    for p in manifest.panels:
        _verify_panel_disk_image(chap_dir, p)

    owns_client = client is None
    transport: DecisionClient = client if client is not None else DecisionClient(retry_jitter=0.15)
    model_name = transport.model or DECISIONS_MODEL
    try:
        cache_version = compute_decision_cache_version(model_name, config=cfg)
        panels = manifest.panels
        orig_panels_dump = [p.model_dump(mode="json") for p in panels]
        orig_sheets_dump = [cs.model_dump(mode="json") for cs in manifest.contact_sheets]
        orig_reviewable_ids = [p.panel_id for p in panels if p.status != PanelStatus.SKIP]
        cache_path = chap_dir / "decision_cache.json"
        cached_raw: Dict[str, Any] = {}
        if cache_path.exists():
            try:
                loaded_cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded_cache, dict):
                    cached_raw = loaded_cache
            except Exception:
                cached_raw = {}

        force_refresh = bool(refresh)
        pass1: Dict[str, Dict[str, Any]] = {}
        pass1_records: Dict[str, Dict[str, Any]] = {}
        pass2_records: Dict[str, Dict[str, Any]] = {}
        cache_hits_pass1 = 0

        if not force_refresh:
            for p in panels:
                expect_child = (p.evidence_metadata or {}).get("repack_op") in ("split_at", "merge")
                entry = cached_raw.get(f"__pass2_{p.panel_id}") if expect_child else cached_raw.get(p.panel_id)
                if not entry and not expect_child:
                    entry = cached_raw.get(p.panel_id)
                elif expect_child and not _is_valid_raw_cache_entry(p, entry, cache_version, expect_child_pass2=True):
                    entry = cached_raw.get(p.panel_id)
                if _is_valid_raw_cache_entry(p, entry, cache_version, expect_child_pass2=expect_child) and isinstance(
                    entry.get("decision"), dict
                ):
                    raw_rec = _raw_payload_from_cache_entry(entry)
                    dec_copy = dict(entry["decision"])
                    origin = "child_pass2" if expect_child else str(entry.get("panel_origin") or "raw_pass1")
                    pass1[p.panel_id] = dec_copy
                    pass1_records[p.panel_id] = {
                        "dec": dec_copy,
                        "raw": raw_rec,
                        "sha256": p.sha256,
                        "width": p.width,
                        "height": p.height,
                        "origin": origin,
                        "source_raw_panels": (p.evidence_metadata or {}).get("source_raw_panels"),
                        "source_raw_sha256s": (p.evidence_metadata or {}).get("source_raw_sha256s"),
                    }
                    cache_hits_pass1 += 1

        pending = [p for p in panels if p.panel_id not in pass1]
        resolved_key: Optional[str] = None

        def _get_api_key() -> str:
            nonlocal resolved_key
            if resolved_key is None:
                resolved_key = resolve_gemini_api_key(api_key)
            return resolved_key

        telemetry: Dict[str, Any] = {
            "requests": {},
            "cache_hits": {"pass1": cache_hits_pass1, "split": 0, "merge": 0, "pass2": 0, "crop_verify": 0},
            "tokens_by_phase": {},
            "total_input_tokens": 0,
            "text_tokens": 0,
            "image_tokens": 0,
            "unknown_usage_requests": 0,
            "retries": 0,
            "latencies_ms": [],
            "content_blocked_panels": [],
        }

        cache_lock = threading.Lock()
        cache_payload: Dict[str, Any] = dict(cached_raw)

        def _flush_cache() -> None:
            with cache_lock:
                atomic_write_json(cache_path, cache_payload)

        logger.info(
            f"DecisionEngine Pass 1 ({cfg.policy}): {len(pending)} pending / {len(panels)} total via {model_name}..."
        )
        if pending:
            key = _get_api_key()
            first_error: Optional[BaseException] = None
            completed_since_flush = 0
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(
                            _evaluate_panel_raw,
                            chap_dir / p.relative_path,
                            p.width,
                            p.height,
                            key,
                            client=transport,
                            config=cfg,
                        ): p
                        for p in pending
                    }
                    for fut in as_completed(futures):
                        p = futures[fut]
                        try:
                            raw_res = fut.result()
                            dec = interpret_panel_answers(raw_res, p.width, p.height, config=cfg)
                            is_child = (p.evidence_metadata or {}).get("repack_op") in ("split_at", "merge")
                            origin = "child_pass2" if is_child else "raw_pass1"
                            if is_child:
                                dec["structure_action"] = "ok_single"
                            pass1[p.panel_id] = dec
                            pass1_records[p.panel_id] = {
                                "dec": dict(dec),
                                "raw": raw_res,
                                "sha256": p.sha256,
                                "width": p.width,
                                "height": p.height,
                                "origin": origin,
                                "source_raw_panels": (p.evidence_metadata or {}).get("source_raw_panels"),
                                "source_raw_sha256s": (p.evidence_metadata or {}).get("source_raw_sha256s"),
                            }
                            if raw_res.get("blocked"):
                                telemetry["content_blocked_panels"].append(p.panel_id)
                            else:
                                _record_usage_and_latency(
                                    telemetry, "pass1", raw_res.get("usage"), raw_res.get("_meta")
                                )
                            if raw_res.get("blocked") or _is_complete_raw_answers(raw_res.get("answers")):
                                with cache_lock:
                                    cache_payload[p.panel_id] = {
                                        "sha256": p.sha256,
                                        "width": p.width,
                                        "height": p.height,
                                        "cache_version": cache_version,
                                        "raw_answers": raw_res.get("answers", {}),
                                        "blocked": bool(raw_res.get("blocked")),
                                        "review_reason": raw_res.get("review_reason"),
                                        "panel_origin": origin,
                                        "source_raw_panels": (p.evidence_metadata or {}).get("source_raw_panels"),
                                        "source_raw_sha256s": (p.evidence_metadata or {}).get("source_raw_sha256s"),
                                        "decision": {k: v for k, v in dec.items() if k != "raw_answers"},
                                    }
                            completed_since_flush += 1
                            if completed_since_flush >= 10:
                                completed_since_flush = 0
                                _flush_cache()
                        except BaseException as exc:
                            if first_error is None:
                                first_error = exc
            finally:
                if pass1:
                    _flush_cache()
            if first_error is not None:
                raise first_error

        # Resolve split cut positions with cache
        split_targets = [p for p in panels if pass1[p.panel_id]["structure_action"] == "needs_split"]
        split_cuts: Dict[str, List[float]] = {}
        if split_targets:
            logger.info(f"DecisionEngine: Resolving cut positions for {len(split_targets)} multi-frame panels...")
            to_query_split: List[PanelItem] = []
            for p in split_targets:
                s_key = f"__split_{p.panel_id}"
                cached_s = cache_payload.get(s_key) if not force_refresh else None
                if _is_valid_split_cache_entry(p, cached_s, cache_version):
                    telemetry["cache_hits"]["split"] += 1
                    if cached_s.get("cuts"):
                        split_cuts[p.panel_id] = list(cached_s["cuts"])
                else:
                    to_query_split.append(p)
            if to_query_split:
                key = _get_api_key()
                with ThreadPoolExecutor(max_workers=min(max_workers, len(to_query_split))) as pool:
                    futs = {
                        pool.submit(
                            _choose_cut_ratios_detailed,
                            chap_dir,
                            p.panel_id,
                            p.height / float(max(1, p.width)),
                            key,
                            client=transport,
                            config=cfg,
                        ): p
                        for p in to_query_split
                    }
                    for fut in as_completed(futs):
                        p = futs[fut]
                        sres = fut.result()
                        if sres.get("usage") or (sres.get("_meta") or {}).get("latency_ms"):
                            _record_usage_and_latency(
                                telemetry, "split_select", sres.get("usage"), sres.get("_meta")
                            )
                        if sres.get("cuts"):
                            split_cuts[p.panel_id] = list(sres["cuts"])
                        cache_payload[f"__split_{p.panel_id}"] = {
                            "sha256": p.sha256,
                            "width": p.width,
                            "height": p.height,
                            "cache_version": cache_version,
                            "cuts": sres.get("cuts"),
                            "reason": sres.get("reason"),
                        }
                _flush_cache()

        # Build PanelPlan resolving needs_split and semantically verified merge_with_prev
        plan_items: List[PanelPlanItem] = []
        merge_diagnostics: List[Dict[str, Any]] = []
        for idx, p in enumerate(panels):
            dec = pass1[p.panel_id]
            status = PanelStatus(dec["status"])

            can_try_merge = (
                dec["structure_action"] == "merge_with_prev"
                and idx > 0
                and bool(plan_items)
                and plan_items[-1].op in (PanelPlanOp.KEEP, PanelPlanOp.MERGE)
                and plan_items[-1].group != PanelStatus.SKIP
                and status != PanelStatus.SKIP
            )
            if can_try_merge:
                prev_panel = panels[idx - 1]
                contig_ok, vcs_gap, vcs_reason = _is_vcs_merge_contiguous(
                    prev_panel,
                    p,
                    max_gap_px=cfg.max_merge_vcs_gap_px,
                )
                allow_merge = False
                merge_action = vcs_reason
                if contig_ok:
                    m_key = f"__merge_{prev_panel.panel_id}_{p.panel_id}"
                    cached_m = cache_payload.get(m_key) if not force_refresh else None
                    pair_sig = {
                        "prev_sha256": prev_panel.sha256,
                        "curr_sha256": p.sha256,
                        "prev_size": [prev_panel.width, prev_panel.height],
                        "curr_size": [p.width, p.height],
                        "vcs_gap_px": int(vcs_gap or 0),
                    }
                    if (
                        isinstance(cached_m, dict)
                        and cached_m.get("cache_version") == cache_version
                        and cached_m.get("pair_signature") == pair_sig
                        and isinstance(cached_m.get("result"), dict)
                        and cached_m["result"].get("action") in ("merge", "keep_separate", "uncertain")
                    ):
                        mres = dict(cached_m["result"])
                        telemetry["cache_hits"]["merge"] += 1
                    else:
                        mres = verify_merge_pair_via_decision(
                            chap_dir,
                            prev_panel,
                            p,
                            int(vcs_gap or 0),
                            _get_api_key(),
                            client=transport,
                            config=cfg,
                        )
                        _record_usage_and_latency(
                            telemetry, "merge_verify", mres.get("usage"), mres.get("_meta")
                        )
                        cache_payload[m_key] = {
                            "pair_signature": pair_sig,
                            "cache_version": cache_version,
                            "result": {"action": mres["action"], "confidence": mres.get("confidence")},
                        }
                    merge_action = mres["action"]
                    allow_merge = merge_action == "merge"
                    if merge_action == "uncertain":
                        status = PanelStatus.NEEDS_REVIEW
                        dec["status"] = PanelStatus.NEEDS_REVIEW.value
                        dec["decision_valid"] = False
                        dec["crop_safe"] = False
                        dec["text_detection_uncertain"] = True
                        dec["review_reason"] = "uncertain_merge_boundary"
                merge_diagnostics.append(
                    {
                        "prev_panel_id": prev_panel.panel_id,
                        "curr_panel_id": p.panel_id,
                        "vcs_contiguous": contig_ok,
                        "vcs_gap_px": vcs_gap,
                        "vcs_reason": vcs_reason,
                        "action": merge_action,
                        "merged": allow_merge,
                    }
                )
                if allow_merge:
                    prev_item = plan_items[-1]
                    plan_items[-1] = PanelPlanItem(
                        op=PanelPlanOp.MERGE,
                        source_panel_ids=prev_item.source_panel_ids + [p.panel_id],
                        group=prev_item.group,
                        reason="decision_merge_fragment",
                    )
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

        plan = PanelPlan(
            project_slug=manifest.project_slug,
            chapter_number=manifest.chapter_number,
            items=plan_items,
        )
        atomic_write_json(chap_dir / "panel_plan.json", plan)
        repack_res = apply_panel_plan(chap_dir, plan, regenerate_sheets=False)

        updated_manifest = load_manifest(chap_dir)
        assert updated_manifest is not None
        for p in updated_manifest.panels:
            _verify_panel_disk_image(chap_dir, p)

        # Pass 2: Re-classify newly split/merged child panels (never parent-inherit!)
        to_reclassify: List[PanelItem] = []
        for p in updated_manifest.panels:
            meta = p.evidence_metadata or {}
            if meta.get("repack_op") in ("split_at", "merge"):
                p1_rec = pass1_records.get(p.panel_id)
                already_verified_child = (
                    not force_refresh
                    and repack_res.get("fast_path") == "metadata_only"
                    and p1_rec is not None
                    and p1_rec.get("origin") == "child_pass2"
                    and p1_rec["sha256"] == p.sha256
                    and (p1_rec["width"], p1_rec["height"]) == (p.width, p.height)
                    and p1_rec.get("source_raw_panels") == meta.get("source_raw_panels")
                    and p1_rec.get("source_raw_sha256s") == meta.get("source_raw_sha256s")
                )
                if not already_verified_child:
                    to_reclassify.append(p)

        if to_reclassify:
            logger.info(f"DecisionEngine Pass 2: Classifying {len(to_reclassify)} newly split/merged panels...")
            to_query_p2: List[PanelItem] = []
            for p in to_reclassify:
                c2_key = f"__pass2_{p.panel_id}"
                cached_p2 = cache_payload.get(c2_key) if not force_refresh else None
                if _is_valid_raw_cache_entry(p, cached_p2, cache_version, expect_child_pass2=True) and isinstance(
                    cached_p2.get("decision"), dict
                ):
                    raw_rec = _raw_payload_from_cache_entry(cached_p2)
                    dec_copy = dict(cached_p2["decision"])
                    dec_copy["structure_action"] = "ok_single"
                    pass2_records[p.panel_id] = {
                        "dec": dec_copy,
                        "raw": raw_rec,
                        "sha256": p.sha256,
                        "width": p.width,
                        "height": p.height,
                        "origin": "child_pass2",
                    }
                    telemetry["cache_hits"]["pass2"] += 1
                else:
                    to_query_p2.append(p)
            if to_query_p2:
                key = _get_api_key()
                with ThreadPoolExecutor(max_workers=min(max_workers, len(to_query_p2))) as pool:
                    futs = {
                        pool.submit(
                            _evaluate_panel_raw,
                            chap_dir / p.relative_path,
                            p.width,
                            p.height,
                            key,
                            client=transport,
                            config=cfg,
                        ): p
                        for p in to_query_p2
                    }
                    for fut in as_completed(futs):
                        p = futs[fut]
                        raw_res = fut.result()
                        dec = interpret_panel_answers(raw_res, p.width, p.height, config=cfg)
                        dec["structure_action"] = "ok_single"
                        pass2_records[p.panel_id] = {
                            "dec": dict(dec),
                            "raw": raw_res,
                            "sha256": p.sha256,
                            "width": p.width,
                            "height": p.height,
                            "origin": "child_pass2",
                        }
                        if raw_res.get("blocked"):
                            telemetry["content_blocked_panels"].append(p.panel_id)
                        else:
                            _record_usage_and_latency(
                                telemetry, "pass2", raw_res.get("usage"), raw_res.get("_meta")
                            )
                        if raw_res.get("blocked") or _is_complete_raw_answers(raw_res.get("answers")):
                            cache_payload[f"__pass2_{p.panel_id}"] = {
                                "sha256": p.sha256,
                                "width": p.width,
                                "height": p.height,
                                "cache_version": cache_version,
                                "raw_answers": raw_res.get("answers", {}),
                                "blocked": bool(raw_res.get("blocked")),
                                "review_reason": raw_res.get("review_reason"),
                                "panel_origin": "child_pass2",
                                "source_raw_panels": (p.evidence_metadata or {}).get("source_raw_panels"),
                                "source_raw_sha256s": (p.evidence_metadata or {}).get("source_raw_sha256s"),
                                "decision": {k: v for k, v in dec.items() if k != "raw_answers"},
                            }

        needs_review_panels: List[Dict[str, Any]] = []
        resolved_by_panel_id: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], str]] = {}
        for p in updated_manifest.panels:
            final_dec, raw_rec, origin = _resolve_post_repack_record(p, pass1_records, pass2_records)
            resolved_by_panel_id[p.panel_id] = (final_dec, raw_rec, origin)
            _apply_decision_to_panel(
                p,
                final_dec,
                model_name=model_name,
                cfg=cfg,
                needs_review_panels=needs_review_panels,
            )

        panels_dir = chap_dir / "panels"
        sheets_dir = chap_dir / "contact_sheets"
        reviewable = [p for p in updated_manifest.panels if p.status != PanelStatus.SKIP]
        after_reviewable_ids = [p.panel_id for p in reviewable]
        sheets_present = (
            all((chap_dir / cs.relative_path).exists() for cs in updated_manifest.contact_sheets)
            if (updated_manifest.contact_sheets and after_reviewable_ids)
            else (not after_reviewable_ids)
        )
        need_regen_sheets = (
            repack_res.get("fast_path") != "metadata_only"
            or orig_reviewable_ids != after_reviewable_ids
            or not sheets_present
        )
        if need_regen_sheets:
            for old_s in sheets_dir.glob("*"):
                old_s.unlink(missing_ok=True)
            sheet_gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
            updated_manifest.contact_sheets = sheet_gen.generate_sheets(reviewable, panels_dir, sheets_dir)

        after_panels_dump = [p.model_dump(mode="json") for p in updated_manifest.panels]
        after_sheets_dump = [cs.model_dump(mode="json") for cs in updated_manifest.contact_sheets]
        if (
            repack_res.get("fast_path") != "metadata_only"
            or after_panels_dump != orig_panels_dump
            or after_sheets_dump != orig_sheets_dump
        ):
            updated_manifest.created_at = utc_iso_now()
            atomic_write_json(chap_dir / "manifest.json", updated_manifest)

        final_cache: Dict[str, Any] = {
            k: v for k, v in cache_payload.items() if str(k).startswith("__") and isinstance(v, dict)
        }
        for p in updated_manifest.panels:
            final_dec, raw_rec, origin = resolved_by_panel_id[p.panel_id]
            final_dec = dict(final_dec)
            final_dec.pop("raw_answers", None)
            if not raw_rec.get("blocked") and not _is_complete_raw_answers(raw_rec.get("answers")):
                continue
            cache_entry = {
                "sha256": p.sha256,
                "width": p.width,
                "height": p.height,
                "cache_version": cache_version,
                "raw_answers": raw_rec.get("answers", {}),
                "blocked": bool(raw_rec.get("blocked")),
                "review_reason": raw_rec.get("review_reason"),
                "panel_origin": origin,
                "source_raw_panels": (p.evidence_metadata or {}).get("source_raw_panels"),
                "source_raw_sha256s": (p.evidence_metadata or {}).get("source_raw_sha256s"),
                "decision": final_dec,
            }
            final_cache[p.panel_id] = cache_entry
            if origin == "child_pass2":
                final_cache[f"__pass2_{p.panel_id}"] = dict(cache_entry)
        if final_cache != cached_raw:
            atomic_write_json(cache_path, final_cache)

        counts = {
            "active": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.ACTIVE),
            "context_only": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.CONTEXT_ONLY),
            "skip": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.SKIP),
        }
        needs_review_count = sum(1 for p in updated_manifest.panels if p.status == PanelStatus.NEEDS_REVIEW)
        latencies = sorted(telemetry["latencies_ms"])
        p50_ms = round(statistics.median(latencies), 2) if latencies else None
        p95_ms = (
            round(latencies[min(len(latencies) - 1, max(0, int(round(0.95 * len(latencies))) - 1))], 2)
            if latencies
            else None
        )
        est_usd = round((telemetry["total_input_tokens"] / 1_000_000.0) * cfg.cost_usd_per_1m_input_tokens, 6)

        return {
            "project_slug": updated_manifest.project_slug,
            "chapter_number": updated_manifest.chapter_number,
            "model": model_name,
            "policy": cfg.policy,
            "cache_version": cache_version,
            "fast_path": repack_res.get("fast_path"),
            "pass1_panels": len(panels),
            "split_resolved": len(split_cuts),
            "pass2_reclassified": len(to_reclassify),
            "final_total_panels": len(updated_manifest.panels),
            "counts_by_group": counts,
            "needs_review_count": needs_review_count,
            "needs_review_panels": needs_review_panels,
            "total_contact_sheets": len(updated_manifest.contact_sheets),
            "total_input_tokens": telemetry["total_input_tokens"],
            "text_tokens": telemetry["text_tokens"],
            "image_tokens": telemetry["image_tokens"],
            "tokens_by_phase": telemetry["tokens_by_phase"],
            "requests": telemetry["requests"],
            "cache_hits": telemetry["cache_hits"],
            "retries": telemetry["retries"],
            "unknown_usage_requests": telemetry["unknown_usage_requests"],
            "estimated_cost_usd": est_usd,
            "latency_p50_ms": p50_ms,
            "latency_p95_ms": p95_ms,
            "split_diagnostics": repack_res.get("split_diagnostics", {}),
            "merge_diagnostics": merge_diagnostics,
        }
    finally:
        if owns_client and transport is not None:
            transport.close()
