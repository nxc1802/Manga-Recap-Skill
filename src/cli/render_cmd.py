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
        workers=getattr(args, "workers", None) or 4,
        kaggle=bool(getattr(args, "kaggle", False)),
        cleanup=bool(getattr(args, "cleanup", False)),
    )
