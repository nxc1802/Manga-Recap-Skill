"""
src/cli/tts_benchmark_cmd.py
Subcommand handler: manga-recap tts_benchmark
Runs VieNeu-TTS voice screening and emotion benchmarks with telemetry.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List

from src.tts.benchmark import EMOTION_BENCHMARK_PROMPTS, TTSBenchmarkRunner
from src.tts.voice_profiles import VOICE_CATALOG_25, list_top10_voices


def handle_tts_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    """Runs VieNeu-TTS voice screening and emotion benchmarks."""
    voices_arg = getattr(args, "voices", "all")
    out_dir_str = getattr(args, "out_dir", "data/benchmarks/vieneu_v3_turbo")
    emotions_arg = getattr(args, "emotions", "all")
    repeats = getattr(args, "repeats", 1)
    use_mock = getattr(args, "mock", False)
    copy_to_lab = getattr(args, "copy_to_lab", True)

    # Guard against clobbering full lab dataset with partial voice benchmark
    if copy_to_lab and voices_arg != "all" and not getattr(args, "force_copy_to_lab", False):
        import logging
        logging.getLogger(__name__).warning(
            "Partial voice benchmark detected (%s). Skipping docs/lab copy to prevent clobbering 25-voice dataset. "
            "Use --voices all to update the lab.",
            voices_arg,
        )
        copy_to_lab = False

    out_dir = Path(out_dir_str).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve voices
    if voices_arg == "top10":
        selected_voices = [v.name for v in list_top10_voices()]
    elif voices_arg == "all" or not voices_arg:
        selected_voices = [v.name for v in VOICE_CATALOG_25]
    else:
        selected_voices = [v.strip() for v in voices_arg.split(",") if v.strip()]

    # Resolve emotions
    all_emotion_ids = [ep["id"] for ep in EMOTION_BENCHMARK_PROMPTS]
    if emotions_arg == "all" or not emotions_arg:
        selected_emotions = all_emotion_ids
    else:
        selected_emotions = [e.strip() for e in emotions_arg.split(",") if e.strip()]

    runner = TTSBenchmarkRunner(
        output_dir=out_dir,
        voices=selected_voices,
        emotions=selected_emotions,
        repeats=repeats,
        use_mock=use_mock,
        copy_to_lab=copy_to_lab,
        include_ab_tests=getattr(args, "ab_tests", False),
    )

    result = runner.run()

    # Provide backward-compatible keys
    result["total_voices_tested"] = len(selected_voices)
    result["voices"] = selected_voices
    result["output_directory"] = str(out_dir)
    result["emotion_categories"] = selected_emotions

    return result
