"""TTS CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.execution_policy import enforce_execution_policy
from src.core.workflow import resolve_segment_voice, synthesize_chapter_tts
from src.tts.engine import get_tts_engine

__all__ = ["handle_tts", "resolve_segment_voice", "get_tts_engine"]


def handle_tts(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap tts`` with injectable engine factory for tests."""
    enforce_execution_policy("tts", args)

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required for TTS synthesis.")

    if getattr(args, "mock", False) or getattr(args, "engine", "neural") != "neural":
        raise CliValidationError("Production TTS requires the real neural engine")

    no_cache = bool(getattr(args, "no_cache", False))
    force_resynth = bool(getattr(args, "force", False)) or no_cache

    return synthesize_chapter_tts(
        project_slug,
        str(chapter_num),
        force=force_resynth,
        no_cache=no_cache,
        engine=getattr(args, "engine", "neural"),
        workers=getattr(args, "workers", None) or 2,
        kaggle=bool(getattr(args, "kaggle", False)),
        mock=bool(getattr(args, "mock", False)),
        force_reload=bool(getattr(args, "force_reload", False)),
        tts_engine_factory=get_tts_engine,
    )
