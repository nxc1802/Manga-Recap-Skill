"""Render CLI command handler delegating to core workflow."""

import argparse
from typing import Any, Dict

from src.cli.exit_codes import CliValidationError
from src.core.execution_policy import enforce_execution_policy
from src.core.workflow import render_chapter_workflow


def handle_render(args: argparse.Namespace) -> Dict[str, Any]:
    """Handle ``manga-recap render`` by delegating to chapter rendering workflow."""
    enforce_execution_policy("render", args)

    project_slug = getattr(args, "project", "")
    chapter_num = getattr(args, "chapter", None)
    if not project_slug or chapter_num is None:
        raise CliValidationError("Both --project and --chapter are required for rendering.")

    return render_chapter_workflow(
        project_slug,
        str(chapter_num),
        quality=getattr(args, "quality", "final"),
        encoder=getattr(args, "encoder", None),
        hw_accel=getattr(args, "hw_accel", None),
        workers=getattr(args, "workers", None) or 4,
        bgm=getattr(args, "bgm", None),
        burn_subtitles=getattr(args, "burn_subtitles", None),
        animation=getattr(args, "animation", None),
        video_mode=getattr(args, "video_mode", None),
        subtitle_lines=getattr(args, "subtitle_lines", None),
        subtitle_font_size=getattr(args, "subtitle_font_size", None),
        subtitle_chunking=getattr(args, "subtitle_chunking", None),
        allow_placeholders=bool(getattr(args, "allow_placeholders", False)),
        kaggle=bool(getattr(args, "kaggle", False)),
        cleanup=bool(getattr(args, "cleanup", False)),
    )
