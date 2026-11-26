"""
Compatibility facade re-exporting Gemini Decisions provider & routing helpers.

Authoritative transport lives in ``src.providers.decision`` and domain routing
lives in ``src.ingest.decision``.
"""

from __future__ import annotations

from src.core.settings import resolve_gemini_api_key
from src.ingest.decision import (
    CENTER_CROP_2D_PRESETS,
    CROP_POLICY_VERSION,
    DECISION_QUESTION_VERSION,
    DecisionPolicyConfig,
    build_panel_questions,
    choose_cut_ratios_via_decision,
    compute_decision_cache_version,
    decide_single_panel,
    evaluate_4sided_crop,
    run_decision_pipeline,
)
from src.providers.decision import (
    DECISIONS_ENDPOINT,
    DECISIONS_MODEL,
    DecisionClient,
    ProviderContentBlockedError,
    call_gemini_decision,
    encode_image_b64,
    redact_secret,
)

_resolve_api_key = resolve_gemini_api_key
_encode_image_b64 = encode_image_b64
_build_panel_questions = build_panel_questions
_choose_cut_ratios_via_decision = choose_cut_ratios_via_decision

__all__ = [
    "DECISIONS_ENDPOINT",
    "DECISIONS_MODEL",
    "DECISION_QUESTION_VERSION",
    "CROP_POLICY_VERSION",
    "CENTER_CROP_2D_PRESETS",
    "DecisionPolicyConfig",
    "DecisionClient",
    "ProviderContentBlockedError",
    "call_gemini_decision",
    "encode_image_b64",
    "redact_secret",
    "build_panel_questions",
    "compute_decision_cache_version",
    "evaluate_4sided_crop",
    "decide_single_panel",
    "choose_cut_ratios_via_decision",
    "run_decision_pipeline",
]
