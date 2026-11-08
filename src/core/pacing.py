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
    """
    Computes pre-TTS speech duration estimation for an entire recap script or list of segments.
    Provides diagnostic metrics and soft budget analysis against target duration.
    """
    segments: List[ScriptSegment]
    if isinstance(script, RecapScript):
        segments = script.segments
    else:
        segments = script

    cfg = pacing_config or ScriptPacingConfig()

    total_syllables = 0
    total_characters = 0
    total_estimated_sec = 0.0
    segment_estimates: List[Dict[str, Any]] = []

    for seg in segments:
        text = seg.narrator_text
        clean = sanitize_subtitle_text(text)
        words = clean.split()
        syllables = len(words)
        chars = len(clean)

        commas, ends, ellipses = count_punctuation_pauses(clean)
        pause_sec = (commas * 0.20) + (ends * 0.40) + (ellipses * 0.50)
        speech_sec = syllables / 3.3
        speed_factor = max(0.5, min(3.0, float(speed)))
        seg_duration = round((speech_sec + pause_sec) / speed_factor, 2)

        total_syllables += syllables
        total_characters += chars
        total_estimated_sec += seg_duration

        segment_estimates.append({
            "seq_id": seg.seq_id,
            "syllables": syllables,
            "characters": chars,
            "estimated_sec": seg_duration,
            "text_snippet": (clean[:40] + "...") if len(clean) > 40 else clean,
        })

    total_estimated_sec = round(total_estimated_sec, 2)
    total_estimated_min = round(total_estimated_sec / 60.0, 2)
    avg_sps = round(total_syllables / max(1.0, total_estimated_sec), 2)

    # Budget comparisons
    target_min = cfg.target_total_minutes
    tolerance = cfg.tolerance
    within_budget = True
    deviation_pct = 0.0
    recommendations: List[str] = []

    if target_min is not None and target_min > 0.0:
        target_sec = target_min * 60.0
        deviation_pct = round(((total_estimated_sec - target_sec) / target_sec) * 100.0, 1)

        lower_bound = target_sec * (1.0 - tolerance)
        upper_bound = target_sec * (1.0 + tolerance)

        if total_estimated_sec > upper_bound:
            within_budget = False
            excess_sec = round(total_estimated_sec - target_sec, 1)
            recommendations.append(
                f"Script duration exceeds budget by {excess_sec:.1f}s ({deviation_pct:+.1f}%). "
                "Consider consolidating secondary descriptions or trimming repetitive scene narratives."
            )
        elif total_estimated_sec < lower_bound:
            within_budget = False
            deficit_sec = round(target_sec - total_estimated_sec, 1)
            recommendations.append(
                f"Script duration is below target budget by {deficit_sec:.1f}s ({deviation_pct:+.1f}%). "
                "Consider elaborating on character motivations, emotional stakes, or battle dynamics."
            )
        else:
            recommendations.append(
                f"Script pacing is balanced and within target tolerance range (+/-{tolerance*100:.0f}%)."
            )
    else:
        recommendations.append(
            f"Script pacing mode '{cfg.mode.value}': estimated total duration is {total_estimated_min:.1f} minutes."
        )

    return {
        "mode": cfg.mode.value if hasattr(cfg.mode, "value") else str(cfg.mode),
        "target_total_minutes": target_min,
        "tolerance": tolerance,
        "total_estimated_seconds": total_estimated_sec,
        "total_estimated_minutes": total_estimated_min,
        "total_syllables": total_syllables,
        "total_characters": total_characters,
        "average_syllables_per_second": avg_sps,
        "deviation_percent": deviation_pct,
        "within_budget": within_budget,
        "recommendations": recommendations,
        "segments": segment_estimates,
    }


def format_pacing_report(pacing_result: Dict[str, Any]) -> str:
    """Formats estimated pacing diagnostic dictionary into a user-friendly console report."""
    lines: List[str] = []
    lines.append("=" * 60)
    lines.append(" PRE-TTS SCRIPT PACING DIAGNOSTIC REPORT")
    lines.append("=" * 60)
    lines.append(f"  Pacing Mode       : {pacing_result.get('mode', 'balanced')}")
    lines.append(f"  Total Segments    : {len(pacing_result.get('segments', []))}")
    lines.append(f"  Total Syllables   : {pacing_result.get('total_syllables', 0)}")
    lines.append(f"  Total Characters  : {pacing_result.get('total_characters', 0)}")
    lines.append(f"  Average Syllables/s: {pacing_result.get('average_syllables_per_second', 0.0):.2f}")
    lines.append(f"  Estimated Duration: {pacing_result.get('total_estimated_seconds', 0.0):.1f}s ({pacing_result.get('total_estimated_minutes', 0.0):.2f} min)")

    target_min = pacing_result.get("target_total_minutes")
    if target_min is not None:
        lines.append(f"  Target Duration   : {target_min:.2f} min (tolerance +/-{pacing_result.get('tolerance', 0.2)*100:.0f}%)")
        lines.append(f"  Budget Deviation  : {pacing_result.get('deviation_percent', 0.0):+.1f}%")
        lines.append(f"  Within Budget     : {'YES' if pacing_result.get('within_budget') else 'NO (Warning)'}")

    lines.append("-" * 60)
    lines.append(" RECOMMENDATIONS:")
    for rec in pacing_result.get("recommendations", []):
        lines.append(f"  * {rec}")
    lines.append("=" * 60)

    return "\n".join(lines)
