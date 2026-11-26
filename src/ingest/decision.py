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
from src.core.storage import atomic_write_json, load_manifest
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

VALID_DECISION_POLICIES: Tuple[str, ...] = (
    "legacy_9preset",
    "9preset_safe",
    "4sided_with_motion",
    "4sided",
)

# 9 2D center-crop presets (retained for legacy_9preset Baseline A comparison & migration reading)
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


@dataclass(frozen=True)
class DecisionPolicyConfig:
    """Typed evaluation & guardrail configuration supporting controlled A/B ablations."""

    policy: str = "4sided"
    include_motion_question: Optional[bool] = None
    apply_guardrails: Optional[bool] = None
    verify_high_risk_crop: bool = False
    verify_merges: Optional[bool] = None
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
    verify_high_risk_area_trigger: float = 0.72
    verify_high_risk_edge_pct_trigger: int = 30
    verify_high_risk_conf_trigger: float = 0.82
    verify_high_risk_noul_threshold: float = 0.75
    cost_usd_per_1m_input_tokens: float = DECISION_USD_PER_1M_INPUT_TOKENS

    def __post_init__(self) -> None:
        if self.policy not in VALID_DECISION_POLICIES:
            raise ValueError(f"Unsupported decision policy '{self.policy}'. Expected one of {VALID_DECISION_POLICIES}.")

    @property
    def uses_4sided_crop(self) -> bool:
        return self.policy in ("4sided", "4sided_with_motion")

    @property
    def uses_guardrails(self) -> bool:
        if self.apply_guardrails is not None:
            return bool(self.apply_guardrails)
        return self.policy != "legacy_9preset"

    @property
    def asks_motion_question(self) -> bool:
        if self.include_motion_question is not None:
            return bool(self.include_motion_question)
        return self.policy in ("legacy_9preset", "9preset_safe", "4sided_with_motion")

    @property
    def checks_merge_pairs(self) -> bool:
        if self.verify_merges is not None:
            return bool(self.verify_merges)
        return self.uses_guardrails


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
    *,
    policy: str = "4sided",
    include_motion: Optional[bool] = None,
) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """Builds standardized closed-set questions for single-panel routing and framing."""
    hw = round(height / float(max(1, width)), 2)
    if policy in ("4sided", "4sided_with_motion"):
        ask_motion = (policy == "4sided_with_motion") if include_motion is None else bool(include_motion)
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
        if ask_motion:
            questions["motion_mode"] = {
                "type": "choice",
                "instructions": "Select video camera motion based on aspect ratio after display crop.",
                "criteria": {
                    "static": f"Static hold when cropped H/W <= {SCROLL_VIEWPORT_H_OVER_W}.",
                    "slow_pan_down": f"Slow scroll down only when cropped H/W > {SCROLL_VIEWPORT_H_OVER_W}.",
                },
            }
        return ctx, questions

    ctx = (
        f"Webtoon panel metadata: width={width}px, height={height}px, height/width ratio={hw:.2f}. "
        "Classify this single panel independently for manga recap video production."
    )
    questions = {
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
                "static": f"Keep static when the display crop has H/W <= {SCROLL_VIEWPORT_H_OVER_W}.",
                "slow_pan_down": f"Scroll down only when the display crop has H/W > {SCROLL_VIEWPORT_H_OVER_W}.",
            },
        },
    }
    return ctx, questions


def compute_decision_cache_version(
    model: str = DECISIONS_MODEL,
    *,
    config: Optional[DecisionPolicyConfig] = None,
) -> str:
    """Computes a stable version stamp over model + question contract + preprocess config."""
    cfg = config or DecisionPolicyConfig()
    _, sample_questions = build_panel_questions(
        1000,
        1000,
        policy=cfg.policy,
        include_motion=cfg.asks_motion_question,
    )
    canonical_spec: Dict[str, Any] = {
        "model": model,
        "question_version": DECISION_QUESTION_VERSION,
        "policy": cfg.policy,
        "asks_motion": cfg.asks_motion_question,
        "questions": sample_questions,
        "preprocess": {
            "max_height": cfg.max_height,
            "max_width": cfg.max_width if cfg.policy != "legacy_9preset" else None,
            "jpeg_quality": cfg.jpeg_quality,
            "image_resolution": cfg.image_resolution,
            "adaptive_tall_resolution": cfg.adaptive_tall_resolution,
        },
    }
    if cfg.policy in ("legacy_9preset", "9preset_safe"):
        canonical_spec["crop_presets"] = CENTER_CROP_2D_PRESETS
    else:
        canonical_spec["crop_policy_version"] = CROP_POLICY_VERSION
        canonical_spec["crop_pct_options"] = list(CROP_PCT_OPTIONS)
    digest = hashlib.sha256(
        json.dumps(canonical_spec, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return f"{model}:{DECISION_QUESTION_VERSION}:{cfg.policy}:{digest}"


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

    if conf is not None and conf < edge_conf_threshold:
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
            apply_guardrails=cfg.uses_guardrails,
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
                edge_supports[s] if edge_supports[s] is not None else 1.0,
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
    Pure, deterministic interpretation over raw API answers so policy thresholds can be
    re-evaluated cleanly via `--reapply-policy` without re-calling Gemini Decisions API.
    """
    cfg = config or DecisionPolicyConfig()
    if raw_payload.get("blocked"):
        full_box = dict(CENTER_CROP_2D_PRESETS["full"])
        return {
            "panel_group": "needs_review",
            "status": PanelStatus.NEEDS_REVIEW.value,
            "structure_action": "ok_single",
            "center_crop_2d": "full" if not cfg.uses_4sided_crop else None,
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
            "crop_method": "4sided_pct" if cfg.uses_4sided_crop else "legacy_9preset",
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

    if not cfg.uses_guardrails:
        # Frozen Baseline A compatibility mode
        eff_group = group_key if valid_group else "group_1_story_art"
        eff_struct = struct_key if valid_struct else "ok_single"
        status = group_map[eff_group]
        crop_key = answers.get("center_crop_2d", {}).get("choice", "full")
        if crop_key not in CENTER_CROP_2D_PRESETS:
            crop_key = "full"
        motion_key = answers.get("motion_mode", {}).get("choice", "static")
        if status != PanelStatus.ACTIVE:
            crop_key = "full"
            motion_key = "static"
        hw = height / float(max(1, width))
        if eff_struct == "needs_split" and (status != PanelStatus.ACTIVE or hw < cfg.split_min_hw):
            eff_struct = "ok_single"
        return {
            "panel_group": eff_group,
            "status": status.value,
            "structure_action": eff_struct,
            "center_crop_2d": crop_key,
            "display_crop": dict(CENTER_CROP_2D_PRESETS[crop_key]),
            "motion_mode": "scroll" if motion_key == "slow_pan_down" else "static",
            "decision_valid": valid_group and valid_struct,
            "crop_safe": True,
            "text_detection_uncertain": False,
            "group_confidence": round(group_conf, 4) if group_conf is not None else None,
            "structure_confidence": round(struct_conf, 4) if struct_conf is not None else None,
            "crop_confidence": None,
            "crop_method": "legacy_9preset",
            "review_reason": None,
            "usage": raw_payload.get("usage", {}),
            "_meta": raw_payload.get("_meta", {}),
        }

    # Guarded modes (9preset_safe, 4sided_with_motion, 4sided)
    low_group_conf = group_conf is not None and group_conf < cfg.group_conf_threshold
    if not valid_group or not valid_struct or low_group_conf:
        reason = (
            "invalid_group_or_structure"
            if not (valid_group and valid_struct)
            else f"low_group_confidence:{group_conf:.3f}"
        )
        full_box = dict(CENTER_CROP_2D_PRESETS["full"])
        return {
            "panel_group": group_key or "uncertain",
            "status": PanelStatus.NEEDS_REVIEW.value,
            "structure_action": "ok_single",
            "center_crop_2d": "full" if not cfg.uses_4sided_crop else None,
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
            "crop_method": "4sided_pct" if cfg.uses_4sided_crop else "legacy_9preset",
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
        full_box = dict(CENTER_CROP_2D_PRESETS["full"])
        return {
            "panel_group": str(group_key),
            "status": status.value,
            "structure_action": eff_struct if eff_struct == "merge_with_prev" else "ok_single",
            "center_crop_2d": "full" if not cfg.uses_4sided_crop else None,
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
            "crop_method": "4sided_pct" if cfg.uses_4sided_crop else "legacy_9preset",
            "review_reason": None,
            "usage": raw_payload.get("usage", {}),
            "_meta": raw_payload.get("_meta", {}),
        }

    if cfg.uses_4sided_crop:
        crop_res = evaluate_4sided_crop(answers, width, height, config=cfg)
        if cfg.asks_motion_question and isinstance(answers.get("motion_mode"), dict):
            m_choice = answers["motion_mode"].get("choice")
            motion_mode = "scroll" if m_choice == "slow_pan_down" else "static"
        else:
            motion_mode = crop_res["motion_mode"]
        return {
            "panel_group": str(group_key),
            "status": PanelStatus.ACTIVE.value,
            "structure_action": eff_struct,
            "center_crop_2d": None,
            "crop_edges_pct": crop_res["crop_edges_pct"],
            "raw_crop_edges_pct": crop_res["raw_crop_edges_pct"],
            "display_crop": crop_res["display_crop"],
            "retained_width": crop_res["retained_width"],
            "retained_height": crop_res["retained_height"],
            "retained_area": crop_res["retained_area"],
            "guardrail_adjusted": crop_res["guardrail_adjusted"],
            "motion_mode": motion_mode if crop_res["decision_valid"] else "static",
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

    # 9preset_safe (Baseline B ablation: 9 presets + strict confidence/safety)
    crop_obj = answers.get("center_crop_2d") if isinstance(answers.get("center_crop_2d"), dict) else {}
    crop_key = crop_obj.get("choice")
    crop_conf_raw = crop_obj.get("confidence")
    crop_conf = (
        float(crop_conf_raw)
        if isinstance(crop_conf_raw, (int, float)) and not isinstance(crop_conf_raw, bool)
        else None
    )
    if not isinstance(crop_key, str) or crop_key not in CENTER_CROP_2D_PRESETS:
        crop_key = "full"
        crop_safe = False
    elif crop_key != "full" and crop_conf is not None and crop_conf < cfg.edge_conf_threshold:
        crop_key = "full"
        crop_safe = True
    else:
        crop_safe = True
    motion_key = answers.get("motion_mode", {}).get("choice", "static")
    return {
        "panel_group": str(group_key),
        "status": PanelStatus.ACTIVE.value,
        "structure_action": eff_struct,
        "center_crop_2d": crop_key,
        "display_crop": dict(CENTER_CROP_2D_PRESETS[crop_key]),
        "motion_mode": "scroll" if motion_key == "slow_pan_down" else "static",
        "decision_valid": True,
        "crop_safe": crop_safe,
        "text_detection_uncertain": not crop_safe,
        "group_confidence": round(group_conf, 4) if group_conf is not None else None,
        "structure_confidence": round(struct_conf, 4) if struct_conf is not None else None,
        "crop_confidence": round(crop_conf, 4) if crop_conf is not None else None,
        "crop_method": "legacy_9preset",
        "review_reason": None,
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
    ctx, questions = build_panel_questions(
        width,
        height,
        policy=cfg.policy,
        include_motion=cfg.asks_motion_question,
    )
    eff_resolution = _resolve_panel_resolution(width, height, cfg)
    try:
        res = call_gemini_decision(
            image_path=image_path,
            text_context=ctx,
            questions=questions,
            api_key=api_key,
            max_height=cfg.max_height,
            max_width=cfg.max_width if cfg.policy != "legacy_9preset" else None,
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
    """Runs Decision 1 evaluation for a single panel image using the configured policy."""
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


def _build_crop_verify_composite_b64(
    image_path: Path,
    display_crop: Dict[str, float],
    *,
    max_height: int = 1024,
) -> Dict[str, Any]:
    """Builds a side-by-side composite (`original + proposed box | candidate crop`) for high-risk verification."""
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        left = rgb.copy()
        draw = ImageDraw.Draw(left)
        x0 = int(round(display_crop["x_min"] * w))
        y0 = int(round(display_crop["y_min"] * h))
        x1 = min(w - 1, max(x0 + 1, int(round(display_crop["x_max"] * w))))
        y1 = min(h - 1, max(y0 + 1, int(round(display_crop["y_max"] * h))))
        draw.rectangle([x0, y0, x1, y1], outline=(0, 220, 80), width=max(4, w // 120))
        right = rgb.crop((x0, y0, min(w, x1 + 1), min(h, y1 + 1)))

        target_h = min(max_height, max(h, right.height))
        scale_l = target_h / float(max(1, left.height))
        left_r = left.resize((max(1, int(round(left.width * scale_l))), target_h), Image.Resampling.LANCZOS)
        scale_r = target_h / float(max(1, right.height))
        right_r = right.resize((max(1, int(round(right.width * scale_r))), target_h), Image.Resampling.LANCZOS)

        gap = 12
        comp = Image.new("RGB", (left_r.width + gap + right_r.width, target_h), (24, 24, 24))
        comp.paste(left_r, (0, 0))
        comp.paste(right_r, (left_r.width + gap, 0))
        buf = io.BytesIO()
        comp.save(buf, format="JPEG", quality=82)
        import base64

        raw_bytes = buf.getvalue()
        return {
            "b64": base64.b64encode(raw_bytes).decode("ascii"),
            "jpeg_bytes": len(raw_bytes),
        }


def verify_crop_safety_via_decision(
    image_path: Path,
    crop_result: Dict[str, Any],
    api_key: str,
    *,
    client: DecisionClient,
    config: DecisionPolicyConfig,
) -> Dict[str, Any]:
    """Runs an optional single `noul` verification pass for aggressive or borderline crops."""
    enc = _build_crop_verify_composite_b64(
        image_path,
        crop_result["display_crop"],
        max_height=config.max_height,
    )
    img_block: Dict[str, Any] = {"type": "image", "data": enc["b64"], "mime_type": "image/jpeg"}
    if config.image_resolution:
        img_block["resolution"] = config.image_resolution
    questions = {
        "crop_preserves_subject": {
            "type": "noul",
            "instructions": (
                "Left shows the original panel with a green crop box; right shows the cropped result. "
                "Does the cropped result preserve all faces, characters, main actions, and essential plot text?"
            ),
            "criteria": {
                "true_description": "All faces, actions, and vital text remain completely intact.",
                "false_description": "A face, limb/action detail, or essential plot speech bubble is cut off or unclear.",
            },
        }
    }
    res = client.call_input(
        input_blocks=[img_block, {"type": "text", "text": f"Verify crop edges={crop_result.get('crop_edges_pct')}."}],
        questions=questions,
        api_key=api_key,
        payload_bytes=enc["jpeg_bytes"],
    )
    p_ok = float(res["answers"]["crop_preserves_subject"]["noul"])
    return {
        "passed": p_ok >= config.verify_high_risk_noul_threshold,
        "p_ok": round(p_ok, 4),
        "usage": res.get("usage", {}),
        "_meta": res.get("_meta", {}),
    }


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
    # In guarded/4sided modes ALWAYS offer no_split even if H/W >= 4.0 so continuous art is never forced-cut
    if cfg.uses_guardrails or hw_ratio < 4.0:
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

    # Validate minimum child height constraints when guardrails are active
    if cfg.uses_guardrails and chosen:
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


def run_decision_pipeline(
    chapter_dir: Path,
    *,
    api_key: Optional[str] = None,
    max_workers: int = 3,
    reclassify_all: bool = False,
    refresh_decisions: bool = False,
    reapply_policy: bool = False,
    dry_run: bool = False,
    client: Optional[DecisionClient] = None,
    config: Optional[DecisionPolicyConfig] = None,
) -> Dict[str, Any]:
    """
    2-Pass non-duplicative Decision API pipeline:
      Pass 1: Evaluate raw panels (group 1/2/3, split/merge candidate, display crop, motion).
      Split/Merge: Guarded resolution via VCS continuity + cut/merge checks.
      Pass 2: Re-classify ONLY newly created split/merged child panels (0 duplicate calls on unchanged).
    """
    cfg = config or DecisionPolicyConfig()
    chap_dir = Path(chapter_dir)
    manifest = load_manifest(chap_dir)
    if manifest is None or not manifest.panels:
        raise ValueError(f"Missing or empty manifest.json in {chap_dir}; run 'segment' first.")

    owns_client = client is None
    transport = client or DecisionClient(retry_jitter=0.15)
    try:
        cache_version = compute_decision_cache_version(transport.model, config=cfg)
        panels = manifest.panels
        cache_path = chap_dir / "decision_cache.json"
        cached_raw: Dict[str, Any] = {}
        if cache_path.exists():
            try:
                loaded_cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(loaded_cache, dict):
                    cached_raw = loaded_cache
            except Exception:
                cached_raw = {}

        force_refresh = bool(refresh_decisions or reclassify_all) and not reapply_policy
        pass1_raw: Dict[str, Dict[str, Any]] = {}
        pass1: Dict[str, Dict[str, Any]] = {}
        cache_hits_pass1 = 0
        if not force_refresh:
            for p in panels:
                entry = cached_raw.get(p.panel_id)
                if not isinstance(entry, dict) or entry.get("sha256") != p.sha256:
                    continue
                if reapply_policy and isinstance(entry.get("raw_answers"), dict) and entry["raw_answers"]:
                    raw_rec = {"answers": entry["raw_answers"], "usage": {}, "_meta": {}}
                    pass1_raw[p.panel_id] = raw_rec
                    pass1[p.panel_id] = interpret_panel_answers(raw_rec, p.width, p.height, config=cfg)
                    cache_hits_pass1 += 1
                elif entry.get("cache_version") == cache_version and isinstance(entry.get("decision"), dict):
                    raw_ans = entry.get("raw_answers") if isinstance(entry.get("raw_answers"), dict) else {}
                    pass1_raw[p.panel_id] = {"answers": raw_ans, "usage": {}}
                    pass1[p.panel_id] = dict(entry["decision"])
                    cache_hits_pass1 += 1

        pending = [p for p in panels if p.panel_id not in pass1]
        if dry_run:
            return {
                "dry_run": True,
                "project_slug": manifest.project_slug,
                "chapter_number": manifest.chapter_number,
                "model": transport.model,
                "policy": cfg.policy,
                "cache_version": cache_version,
                "total_panels": len(panels),
                "pass1_cached": cache_hits_pass1,
                "pass1_pending": len(pending),
                "reapply_policy": reapply_policy,
                "refresh_decisions": force_refresh,
            }

        need_api = bool(pending) or force_refresh
        key = resolve_gemini_api_key(api_key) if (need_api or not reapply_policy) else (api_key or "")

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

        logger.info(
            f"DecisionEngine Pass 1 ({cfg.policy}): {len(pending)} pending / {len(panels)} total via {transport.model}..."
        )
        cache_lock = threading.Lock()
        cache_payload: Dict[str, Any] = dict(cached_raw)

        def _flush_cache() -> None:
            with cache_lock:
                atomic_write_json(cache_path, cache_payload)

        if pending:
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
                            dec["raw_answers"] = raw_res.get("answers", {})
                            pass1_raw[p.panel_id] = raw_res
                            pass1[p.panel_id] = dec
                            if raw_res.get("blocked"):
                                telemetry["content_blocked_panels"].append(p.panel_id)
                            else:
                                _record_usage_and_latency(telemetry, "pass1", raw_res.get("usage"), raw_res.get("_meta"))
                            with cache_lock:
                                cache_payload[p.panel_id] = {
                                    "sha256": p.sha256,
                                    "width": p.width,
                                    "height": p.height,
                                    "cache_version": cache_version,
                                    "raw_answers": raw_res.get("answers", {}),
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

        # Optional Policy E verification on high-risk aggressive 4-sided crops
        if cfg.verify_high_risk_crop and cfg.uses_4sided_crop and not reapply_policy:
            hr_targets: List[PanelItem] = []
            for p in panels:
                dec = pass1[p.panel_id]
                if dec["status"] != PanelStatus.ACTIVE.value or not dec.get("crop_safe"):
                    continue
                edges = dec.get("crop_edges_pct") or {}
                if not any(int(v or 0) > 0 for v in edges.values()):
                    continue
                area_f = float(dec.get("retained_area") or 1.0)
                max_edge = max(int(v or 0) for v in edges.values())
                min_conf = (dec.get("crop_confidence") or {}).get("min_edge")
                if (
                    area_f <= cfg.verify_high_risk_area_trigger
                    or max_edge >= cfg.verify_high_risk_edge_pct_trigger
                    or (min_conf is not None and float(min_conf) < cfg.verify_high_risk_conf_trigger)
                ):
                    hr_targets.append(p)
            if hr_targets:
                for p in hr_targets:
                    v_key = f"__cverify_{p.panel_id}"
                    cached_v = cache_payload.get(v_key) if not force_refresh else None
                    if (
                        isinstance(cached_v, dict)
                        and cached_v.get("sha256") == p.sha256
                        and cached_v.get("cache_version") == cache_version
                        and cached_v.get("crop_edges_pct") == pass1[p.panel_id].get("crop_edges_pct")
                    ):
                        vres = dict(cached_v["result"])
                        telemetry["cache_hits"]["crop_verify"] += 1
                    else:
                        vres = verify_crop_safety_via_decision(
                            chap_dir / p.relative_path,
                            pass1[p.panel_id],
                            key,
                            client=transport,
                            config=cfg,
                        )
                        _record_usage_and_latency(telemetry, "crop_verify", vres.get("usage"), vres.get("_meta"))
                        cache_payload[v_key] = {
                            "sha256": p.sha256,
                            "cache_version": cache_version,
                            "crop_edges_pct": pass1[p.panel_id].get("crop_edges_pct"),
                            "result": {"passed": vres["passed"], "p_ok": vres["p_ok"]},
                        }
                    if not vres["passed"]:
                        dec = pass1[p.panel_id]
                        dec["crop_edges_pct"] = {"left": 0, "top": 0, "right": 0, "bottom": 0}
                        dec["display_crop"] = dict(CENTER_CROP_2D_PRESETS["full"])
                        dec["retained_width"] = dec["retained_height"] = dec["retained_area"] = 1.0
                        dec["guardrail_adjusted"] = True
                        dec["motion_mode"] = compute_deterministic_motion(p.width, p.height, dec["display_crop"])
                        dec["crop_verify_failed"] = True
                _flush_cache()

        # Resolve split cut positions with cache
        split_targets = [p for p in panels if pass1[p.panel_id]["structure_action"] == "needs_split"]
        split_cuts: Dict[str, List[float]] = {}
        if split_targets:
            logger.info(f"DecisionEngine: Resolving cut positions for {len(split_targets)} multi-frame panels...")
            to_query_split: List[PanelItem] = []
            for p in split_targets:
                s_key = f"__split_{p.panel_id}"
                cached_s = cache_payload.get(s_key) if (not force_refresh and not reapply_policy) else None
                if (
                    isinstance(cached_s, dict)
                    and cached_s.get("sha256") == p.sha256
                    and cached_s.get("cache_version") == cache_version
                ):
                    telemetry["cache_hits"]["split"] += 1
                    if cached_s.get("cuts"):
                        split_cuts[p.panel_id] = list(cached_s["cuts"])
                elif not reapply_policy:
                    to_query_split.append(p)
            if to_query_split:
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
                            _record_usage_and_latency(telemetry, "split_select", sres.get("usage"), sres.get("_meta"))
                        if sres.get("cuts"):
                            split_cuts[p.panel_id] = list(sres["cuts"])
                        cache_payload[f"__split_{p.panel_id}"] = {
                            "sha256": p.sha256,
                            "cache_version": cache_version,
                            "cuts": sres.get("cuts"),
                            "reason": sres.get("reason"),
                        }
                _flush_cache()

        # Build PanelPlan resolving needs_split and guarded merge_with_prev
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
                allow_merge = contig_ok
                merge_action = "assumed_contiguous"
                if contig_ok and cfg.checks_merge_pairs and not reapply_policy:
                    m_key = f"__merge_{prev_panel.panel_id}_{p.panel_id}"
                    cached_m = cache_payload.get(m_key) if not force_refresh else None
                    pair_sha = f"{prev_panel.sha256[:16]}_{p.sha256[:16]}"
                    if (
                        isinstance(cached_m, dict)
                        and cached_m.get("sha256") == pair_sha
                        and cached_m.get("cache_version") == cache_version
                    ):
                        mres = dict(cached_m["result"])
                        telemetry["cache_hits"]["merge"] += 1
                    else:
                        mres = verify_merge_pair_via_decision(
                            chap_dir,
                            prev_panel,
                            p,
                            int(vcs_gap or 0),
                            key,
                            client=transport,
                            config=cfg,
                        )
                        _record_usage_and_latency(telemetry, "merge_verify", mres.get("usage"), mres.get("_meta"))
                        cache_payload[m_key] = {
                            "sha256": pair_sha,
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
                        "action": merge_action if contig_ok else vcs_reason,
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

        # Pass 2: Re-classify ONLY newly split or merged child panels (single final sheet generation afterward)
        updated_manifest = load_manifest(chap_dir)
        assert updated_manifest is not None
        to_reclassify = [
            p
            for p in updated_manifest.panels
            if (reclassify_all and not cfg.uses_4sided_crop and not reapply_policy)
            or p.evidence_metadata.get("repack_op") in ("split_at", "merge")
        ]
        pass2: Dict[str, Dict[str, Any]] = {}
        if to_reclassify and not reapply_policy:
            logger.info(f"DecisionEngine Pass 2: Classifying {len(to_reclassify)} newly split/merged panels...")
            to_query_p2: List[PanelItem] = []
            for p in to_reclassify:
                c2_key = f"__pass2_{p.panel_id}"
                cached_p2 = cache_payload.get(c2_key) if not force_refresh else None
                if (
                    isinstance(cached_p2, dict)
                    and cached_p2.get("sha256") == p.sha256
                    and cached_p2.get("cache_version") == cache_version
                    and isinstance(cached_p2.get("decision"), dict)
                ):
                    pass2[p.panel_id] = dict(cached_p2["decision"])
                    telemetry["cache_hits"]["pass2"] += 1
                else:
                    to_query_p2.append(p)
            if to_query_p2:
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
                        pass2[p.panel_id] = dec
                        if raw_res.get("blocked"):
                            telemetry["content_blocked_panels"].append(p.panel_id)
                        else:
                            _record_usage_and_latency(telemetry, "pass2", raw_res.get("usage"), raw_res.get("_meta"))
                        cache_payload[f"__pass2_{p.panel_id}"] = {
                            "sha256": p.sha256,
                            "cache_version": cache_version,
                            "raw_answers": raw_res.get("answers", {}),
                            "decision": {k: v for k, v in dec.items() if k != "raw_answers"},
                        }

        needs_review_panels: List[Dict[str, Any]] = []
        for p in updated_manifest.panels:
            if p.panel_id in pass2:
                dec = pass2[p.panel_id]
            else:
                src_raw = (p.evidence_metadata.get("source_raw_panels") or [p.panel_id])[0]
                dec = pass1[src_raw]

            p.status = PanelStatus(dec["status"])
            p.evidence_metadata["text_detection_uncertain"] = bool(dec.get("text_detection_uncertain", False))
            p.evidence_metadata["crop_safe"] = bool(dec.get("crop_safe", True))
            p.evidence_metadata["decision_valid"] = bool(dec.get("decision_valid", True))
            p.evidence_metadata["decision_model"] = transport.model
            p.evidence_metadata["decision_policy"] = cfg.policy
            p.evidence_metadata["panel_group"] = dec["panel_group"]
            if dec.get("group_confidence") is not None:
                p.evidence_metadata["group_confidence"] = dec["group_confidence"]

            if p.status == PanelStatus.ACTIVE:
                if dec.get("center_crop_2d") is not None:
                    p.evidence_metadata["center_crop_preset"] = dec["center_crop_2d"]
                else:
                    p.evidence_metadata.pop("center_crop_preset", None)
                if dec.get("crop_edges_pct") is not None:
                    p.evidence_metadata["crop_edges_pct"] = dec["crop_edges_pct"]
                if dec.get("raw_crop_edges_pct") is not None:
                    p.evidence_metadata["raw_crop_edges_pct"] = dec["raw_crop_edges_pct"]
                p.evidence_metadata["crop_method"] = dec.get("crop_method", "legacy_9preset")
                if dec.get("crop_confidence") is not None:
                    p.evidence_metadata["crop_confidence"] = dec["crop_confidence"]
                p.evidence_metadata["crop_policy_version"] = CROP_POLICY_VERSION if cfg.uses_4sided_crop else DECISION_QUESTION_VERSION
                p.evidence_metadata["display_crop"] = dec["display_crop"]
                p.evidence_metadata["tall_motion_hint"] = dec["motion_mode"]
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
                    p.notes = f"text_only | decision:{dec['panel_group']}"
                elif p.status == PanelStatus.SKIP:
                    p.notes = f"skip | decision:{dec['panel_group']}"
                else:
                    reason = dec.get("review_reason") or "uncertain_decision"
                    p.evidence_metadata["review_reason"] = reason
                    p.notes = f"needs_review | decision:{reason}"
                    needs_review_panels.append({"panel_id": p.panel_id, "reason": reason})

        panels_dir = chap_dir / "panels"
        sheets_dir = chap_dir / "contact_sheets"
        for old_s in sheets_dir.glob("*"):
            old_s.unlink(missing_ok=True)
        reviewable = [p for p in updated_manifest.panels if p.status != PanelStatus.SKIP]
        sheet_gen = ContactSheetGenerator(cell_size=480, columns=4, rows=4)
        updated_manifest.contact_sheets = sheet_gen.generate_sheets(reviewable, panels_dir, sheets_dir)
        updated_manifest.created_at = utc_iso_now()
        atomic_write_json(chap_dir / "manifest.json", updated_manifest)

        final_cache: Dict[str, Any] = {
            k: v for k, v in cache_payload.items() if str(k).startswith("__") and isinstance(v, dict)
        }
        for p in updated_manifest.panels:
            if p.panel_id in pass2:
                final_dec = dict(pass2[p.panel_id])
            else:
                src_raw = (p.evidence_metadata.get("source_raw_panels") or [p.panel_id])[0]
                final_dec = dict(pass1[src_raw])
                if p.evidence_metadata.get("repack_op") in ("split_at", "merge"):
                    final_dec["structure_action"] = "ok_single"
            raw_ans = final_dec.pop("raw_answers", None)
            if raw_ans is None:
                src_raw = (p.evidence_metadata.get("source_raw_panels") or [p.panel_id])[0]
                raw_ans = (pass1_raw.get(src_raw) or {}).get("answers", {})
            final_cache[p.panel_id] = {
                "sha256": p.sha256,
                "width": p.width,
                "height": p.height,
                "cache_version": cache_version,
                "raw_answers": raw_ans,
                "decision": final_dec,
            }
        atomic_write_json(cache_path, final_cache)

        counts = {
            "active": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.ACTIVE),
            "context_only": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.CONTEXT_ONLY),
            "skip": sum(1 for p in updated_manifest.panels if p.status == PanelStatus.SKIP),
        }
        needs_review_count = sum(1 for p in updated_manifest.panels if p.status == PanelStatus.NEEDS_REVIEW)
        latencies = sorted(telemetry["latencies_ms"])
        p50_ms = round(statistics.median(latencies), 2) if latencies else None
        p95_ms = round(latencies[min(len(latencies) - 1, max(0, int(round(0.95 * len(latencies))) - 1))], 2) if latencies else None
        est_usd = round((telemetry["total_input_tokens"] / 1_000_000.0) * cfg.cost_usd_per_1m_input_tokens, 6)

        return {
            "project_slug": updated_manifest.project_slug,
            "chapter_number": updated_manifest.chapter_number,
            "model": transport.model,
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
        if owns_client:
            transport.close()
