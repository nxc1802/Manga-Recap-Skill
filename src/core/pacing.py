"""
src/core/pacing.py
Pre-TTS Script Pacing & Duration Budget Estimator (PR-03).
Uses syllable counts and empirical punctuation pause modeling:
    T_est = (Syllables / 3.3 + 0.2 * N_commas + 0.4 * N_ends + 0.5 * N_ellipses) / speed
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

from src.core.schemas import PacingMode, RecapScript, ScriptPacingConfig, ScriptSegment
from src.render.subtitle import sanitize_subtitle_text


def count_punctuation_pauses(text: str) -> Tuple[int, int, int]:
    """
    Counts commas, sentence ends, and ellipses without double counting.

    Returns:
        Tuple of (commas_count, sentence_ends_count, ellipses_count)
    """
    # Count and remove ellipses first to prevent double counting dots
    ellipses = len(re.findall(r"\.{3,}|…", text))
    text_clean = re.sub(r"\.{3,}|…", "", text)

    # Commas, semicolons, and dashes
    commas = len(re.findall(r"[,;—]", text_clean))

    # Sentence ends (. ! ?)
    ends = len(re.findall(r"[.!?]", text_clean))

    return commas, ends, ellipses


def estimate_segment_duration_seconds(
    text: str,
    speed: float = 1.0,
) -> float:
    """
    Estimates utterance speech duration in seconds for a Vietnamese text string.
    In Vietnamese, each space-delimited token corresponds to a syllable.
    """
    clean_text = sanitize_subtitle_text(text)
    if not clean_text:
        return 0.0

    words = clean_text.split()
    syllables = len(words)
    if syllables == 0:
        return 0.0

    commas, ends, ellipses = count_punctuation_pauses(clean_text)
    pause_sec = (commas * 0.20) + (ends * 0.40) + (ellipses * 0.50)
    speech_sec = syllables / 3.3
    speed_factor = max(0.5, min(3.0, float(speed)))

    return round((speech_sec + pause_sec) / speed_factor, 2)


def estimate_script_duration_seconds(
    script: Union[RecapScript, List[ScriptSegment]],
    speed: float = 1.0,
    pacing_config: Optional[ScriptPacingConfig] = None,
) -> Dict[str, Any]:
    """Computes essential pre-TTS speech duration estimate and budget check."""
    segments = script.segments if isinstance(script, RecapScript) else script
    cfg = pacing_config or ScriptPacingConfig()

    total_syllables = 0
    total_estimated_sec = 0.0
    for seg in segments:
        clean = sanitize_subtitle_text(seg.narrator_text)
        syllables = len(clean.split())
        total_syllables += syllables
        total_estimated_sec += estimate_segment_duration_seconds(clean, speed=speed)

    total_estimated_sec = round(total_estimated_sec, 2)
    total_estimated_min = round(total_estimated_sec / 60.0, 2)
    target_min = cfg.target_total_minutes
    tolerance = cfg.tolerance
    within_budget = True
    deviation_pct = 0.0
    summary = f"Estimated {total_estimated_min:.1f} min across {len(segments)} segments."

    if target_min is not None and target_min > 0.0:
        target_sec = target_min * 60.0
        deviation_pct = round(((total_estimated_sec - target_sec) / target_sec) * 100.0, 1)
        within_budget = (target_sec * (1.0 - tolerance)) <= total_estimated_sec <= (target_sec * (1.0 + tolerance))
        if not within_budget:
            summary = f"Estimated duration {total_estimated_sec:.1f}s deviates {deviation_pct:+.1f}% from target {target_sec:.1f}s."

    return {
        "mode": cfg.mode.value if hasattr(cfg.mode, "value") else str(cfg.mode),
        "target_total_minutes": target_min,
        "tolerance": tolerance,
        "total_estimated_seconds": total_estimated_sec,
        "total_estimated_minutes": total_estimated_min,
        "total_syllables": total_syllables,
        "within_budget": within_budget,
        "deviation_percent": deviation_pct,
        "recommendations": [summary],
    }
