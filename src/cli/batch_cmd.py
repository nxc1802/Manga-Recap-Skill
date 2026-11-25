"""Batch CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.execution_policy import enforce_execution_policy
from src.core.workflow import (
    batch_chapters,
    discover_project_chapters,
    parse_chapter_list,
)

__all__ = ["handle_batch", "parse_chapter_list", "discover_project_chapters"]


def handle_batch(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap batch`` by delegating to multi-chapter production."""
    enforce_execution_policy("batch", args)

    project_slug = getattr(args, "project", "")
    if not project_slug:
        raise CliValidationError("Project slug (--project) is required for batch processing.")

    tts_workers = getattr(args, "tts_workers", getattr(args, "workers", None))
    render_workers = getattr(args, "render_workers", getattr(args, "workers", None))

    return batch_chapters(
        project_slug,
        chapters=getattr(args, "chapters", None),
        retry_chapter=getattr(args, "retry_chapter", None),
        compile_after=bool(getattr(args, "compile", False)),
        output=getattr(args, "output", None),
        extract_audio=bool(getattr(args, "extract_audio", True)),
        force=bool(getattr(args, "force", False)),
        approved=bool(getattr(args, "approved", False)),
        allow_placeholders=bool(getattr(args, "allow_placeholders", False)),
        voice=getattr(args, "voice", None),
        speed=getattr(args, "speed", None),
        bgm=getattr(args, "bgm", None),
        video_mode=getattr(args, "video_mode", None),
        burn_subtitles=getattr(args, "burn_subtitles", None),
        animation=getattr(args, "animation", None),
        subtitle_lines=getattr(args, "subtitle_lines", None),
        subtitle_font_size=getattr(args, "subtitle_font_size", None),
        subtitle_chunking=getattr(args, "subtitle_chunking", None),
        encoder=getattr(args, "encoder", None),
        hw_accel=getattr(args, "hw_accel", None),
        tts_engine=getattr(args, "engine", getattr(args, "tts_engine", "neural")),
        tts_workers=int(tts_workers) if tts_workers is not None else 2,
        render_workers=int(render_workers) if render_workers is not None else 2,
        cleanup=bool(getattr(args, "cleanup", False)),
        kaggle=bool(getattr(args, "kaggle", False)),
    )
